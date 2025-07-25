# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Attention KV cache modeling."""

# pylint: disable=too-many-statements,too-many-lines,too-many-arguments,invalid-name
import enum
import math
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

import tvm
from tvm import relax as rx
from tvm import tir
from tvm.relax.frontend.nn import Object, Tensor
from tvm.runtime import DataType
from tvm.script import tir as T
from tvm.target import Target

from .position_embedding import llama_rope_with_position_map, switch_rope_freq_func
from .tree_attn import (
    tree_attn,
    tree_attn_cpu,
    tree_attn_with_paged_kv_cache,
    tree_attn_with_paged_kv_cache_cpu,
)


def _var_cpu(dtype):
    return T.alloc_buffer((1,), dtype)


def get_max_num_threads_per_block(target: Target) -> int:
    """
    max(max_num_threads, max_threads_per_block); if latter does not exist, return max_num_threads.
    We add this method since some targets have both fields and `max_threads_per_block` is larger.
    """
    max_num_threads = target.max_num_threads
    max_threads_per_block = target.attrs.get("max_threads_per_block", None)
    if max_threads_per_block is None:
        return max_num_threads
    return max(max_num_threads, max_threads_per_block)


def check_thread_limits(target: Target, bdx: int, bdy: int, bdz: int, gdz: int):
    """
    Check whether max num threads exceeded given a target.

    Parameters
    ----------
    bdx: threadIdx.x
    bdy: threadIdx.y
    bdz: threadIdx.z
    gdz: blockIdx.z
    """
    max_num_threads_per_block = get_max_num_threads_per_block(target)

    assert (
        bdx * bdy * bdz <= max_num_threads_per_block
    ), f"{target.kind} max num threads exceeded: {bdx}*{bdy}*{bdz}>{max_num_threads_per_block}"

    if str(target.kind) == "webgpu":
        # https://gpuweb.github.io/gpuweb/#dom-supported-limits-maxcomputeworkgroupsizez
        assert bdz <= 64, f"webgpu's threadIdx.z cannot exceed 64, but got bdz={bdz}"
        assert gdz == 1, f"webgpu's blockIdx.z should be 1, but got gdz={gdz}"


class AttnKind(enum.IntEnum):
    """The attention kind class.
    MHA denotes multi-head attention, multi-query attention or grouped query attention.
    MLA denotes multi-head latent attention.
    """

    MHA = 0
    MLA = 1
    MHA_SLIDING = 3


class RopeMode(enum.IntEnum):
    """The RoPE mode of the Paged KV cache.
    If it is none, the KV cache will not apply RoPE to q and k.
    If it is normal, RoPE will be applied to k before adding k to cache.
    Otherwise, RoPE will be applied to q/k in attention kernel on-the-fly.
    """

    NONE = 0
    NORMAL = 1
    INLINE = 2


class PagedKVCache(Object):  # pylint: disable=too-few-public-methods
    """The Paged KV Cache used in LLM batching for efficient attention computation."""

    extern_mods: List[tvm.runtime.Module] = []

    def attention_with_fused_qkv(
        self,
        layer_id: int,
        qkv: Tensor,
        num_qo_heads: int,
        sm_scale: float,
    ) -> Tensor:
        """Compute attention with the given fused q/k/v data and in-cache k/v data
        on the specified layer. Rotary position embeddings are applied to k/v
        within this function.

        - For prefill, the input qkv and output tensor have shape
        (1, total_seq_len) for the first two dimensions.
        - For decode, the input qkv and output tensor have shape
        (batch_size, 1) for the first two dimensions.
        - The input qkv have `2 * num_qo_heads + num_kv_heads` at the third dim.
        - The output tensor have `num_qo_heads` at the third dim.
        - The input qkv and output tensor have `head_dim` at the last dim.
        """
        # pylint: disable=protected-access
        b, s, _, d = qkv._expr.struct_info.shape
        qkv = qkv.reshape(b * s, qkv.shape[2], d)
        return Tensor(
            _expr=rx.BlockBuilder.current().emit(
                rx.call_dps_packed(
                    "vm.builtin.attention_kv_cache_attention_with_fused_qkv",
                    [
                        self._expr,
                        rx.PrimValue(layer_id),  # type: ignore[arg-type]
                        rx.PrimValue(sm_scale),
                        qkv._expr,
                    ],
                    out_sinfo=rx.TensorStructInfo((b * s, num_qo_heads, d), qkv.dtype),
                )
            )
        ).reshape(b, s, num_qo_heads, d)

    def self_attention(  # pylint: disable=too-many-locals
        self,
        layer_id: int,
        q: Tensor,
        k: Tensor,
        v: Tensor,
        sm_scale: float,
    ) -> Tuple[Tensor, Tensor]:
        """Fine-grained API that computes ragged self attention with Q/K/V data."""
        # pylint: disable=protected-access
        b, s, h_qo, d_qk = q._expr.struct_info.shape
        _, _, h_kv, d_v = v._expr.struct_info.shape
        q = q.reshape(b * s, h_qo, d_qk)
        k = k.reshape(b * s, h_kv, d_qk)
        v = v.reshape(b * s, h_kv, d_v)
        bb = rx.BlockBuilder.current()
        attn_results = bb.emit(
            rx.call_dps_packed(
                "vm.builtin.attention_kv_cache_self_attention",
                [
                    self._expr,
                    rx.PrimValue(layer_id),  # type: ignore[arg-type]
                    rx.PrimValue(sm_scale),
                    q._expr,
                    k._expr,
                    v._expr,
                ],
                out_sinfo=[
                    rx.TensorStructInfo((b * s, h_qo, d_v), q.dtype),
                    rx.TensorStructInfo((b * s, h_qo), "float32"),
                ],
            )
        )
        assert isinstance(attn_results.struct_info, rx.TupleStructInfo)
        assert len(attn_results.struct_info.fields) == 2
        o = Tensor(_expr=bb.emit(rx.TupleGetItem(attn_results, 0))).reshape(b, s, h_qo, d_v)
        lse = Tensor(_expr=bb.emit(rx.TupleGetItem(attn_results, 1))).reshape(b, s, h_qo)
        return o, lse

    def cross_attention(
        self,
        layer_id: int,
        q: Tensor,
        v_head_dim: int,
        sm_scale: float,
    ) -> Tuple[Tensor, Tensor]:
        """Fine-grained API that computes paged cross attention with Q and in-cache KV data."""
        # pylint: disable=protected-access
        b, s, h_qo, d_qk = q._expr.struct_info.shape
        q = q.reshape(b * s, h_qo, d_qk)
        bb = rx.BlockBuilder.current()
        attn_results = bb.emit(
            rx.call_dps_packed(
                "vm.builtin.attention_kv_cache_cross_attention",
                [
                    self._expr,
                    rx.PrimValue(layer_id),  # type: ignore[arg-type]
                    rx.PrimValue(sm_scale),
                    q._expr,
                ],
                out_sinfo=[
                    rx.TensorStructInfo((b * s, h_qo, v_head_dim), q.dtype),
                    rx.TensorStructInfo((b * s, h_qo), "float32"),
                ],
            )
        )
        assert isinstance(attn_results.struct_info, rx.TupleStructInfo)
        assert len(attn_results.struct_info.fields) == 2
        o = Tensor(_expr=bb.emit(rx.TupleGetItem(attn_results, 0))).reshape(b, s, h_qo, v_head_dim)
        lse = Tensor(_expr=bb.emit(rx.TupleGetItem(attn_results, 1))).reshape(b, s, h_qo)
        return o, lse

    def append_mla_kv(self, layer_id: int, kv: Tensor) -> "PagedKVCache":
        """Fine-grained API that appends the MLA K/V data to KV cache."""
        # pylint: disable=protected-access
        b, s, _, d_qk = kv._expr.struct_info.shape
        kv = kv.reshape(b * s, d_qk)
        return PagedKVCache(
            _expr=rx.call_pure_packed(
                "vm.builtin.attention_kv_cache_append_mla_kv",
                self._expr,
                rx.PrimValue(layer_id),  # type: ignore[arg-type]
                kv._expr,
                sinfo_args=rx.ObjectStructInfo(),
            ),
            _name="paged_kv_cache",
        )

    def merge_attn_output_inplace(
        self,
        o_self_attn: Tensor,
        lse_self_attn: Tensor,
        o_cross_attn: Tensor,
        lse_cross_attn: Tensor,
    ) -> Tuple[Tensor, Tensor]:
        """Fine-grained API that merges the attention output from two sources.
        The first two tensors will be inplace updated.
        """
        # pylint: disable=protected-access
        b, s, h_qo, d_v = o_self_attn._expr.struct_info.shape
        o_self_attn = o_self_attn.reshape(b * s, h_qo, d_v)
        lse_self_attn = lse_self_attn.reshape(b * s, h_qo)
        o_cross_attn = o_cross_attn.reshape(b * s, h_qo, d_v)
        lse_cross_attn = lse_cross_attn.reshape(b * s, h_qo)
        bb = rx.BlockBuilder.current()
        merge_results = bb.emit(
            rx.call_pure_packed(
                "vm.builtin.attention_kv_cache_merge_attn_output_inplace",
                self._expr,
                o_self_attn._expr,
                lse_self_attn._expr,
                o_cross_attn._expr,
                lse_cross_attn._expr,
                sinfo_args=rx.TupleStructInfo(
                    [o_self_attn._expr.struct_info, lse_self_attn._expr.struct_info]
                ),
            )
        )
        assert isinstance(merge_results.struct_info, rx.TupleStructInfo)
        assert len(merge_results.struct_info.fields) == 2
        o_self_attn = Tensor(_expr=bb.emit(rx.TupleGetItem(merge_results, 0))).reshape(
            b, s, h_qo, d_v
        )
        lse_self_attn = Tensor(_expr=bb.emit(rx.TupleGetItem(merge_results, 1))).reshape(b, s, h_qo)
        return o_self_attn, lse_self_attn

    def get_query_positions(self, total_length: tir.PrimExpr) -> Tensor:
        """Get the in-sequence positions of each slot in the query,
        which are needed for applying positional embeddings in some models.

        Parameters
        ----------
        total_length : tir.PrimExpr
            The summed-up total sequence length of queries in
            the batch being forwarded.

        Returns
        -------
        q_positions : Tensor
            The in-sequence query positions, in shape `(total_length,)`
        """
        return Tensor(
            _expr=rx.BlockBuilder.current().emit(
                rx.call_pure_packed(
                    "vm.builtin.attention_kv_cache_get_query_positions",
                    self._expr,
                    sinfo_args=rx.TensorStructInfo((total_length,), "int32"),
                )
            )
        )

    # pylint: enable=protected-access


class FlashInferPagedKVCache(PagedKVCache):  # pylint: disable=too-few-public-methods
    """Paged KV cache using FlashInfer (CUDA) kernels."""

    def __init__(  # pylint: disable=too-many-locals
        self,
        attn_kind: Union[Literal["mha", "mla"], List[Literal["mha", "mla", "mha_sliding"]]],
        max_batch_size: tir.Var,
        max_total_seq_len: tir.Var,
        prefill_chunk_size: tir.Var,
        page_size: tir.Var,
        support_sliding_window: tir.Var,
        layer_partition: rx.ShapeExpr,
        num_hidden_layers: int,
        num_attention_heads: int,
        num_key_value_heads: int,
        qk_head_dim: int,
        v_head_dim: int,
        mla_original_qk_head_dim: int,
        mla_original_v_head_dim: int,
        rope_mode: RopeMode,
        rope_scale: int,
        rope_theta: int,
        rope_scaling: Dict[str, Any],
        rope_ext_factors: rx.Expr,
        rotary_dim: int,
        enable_disaggregation: bool,
        dtype: str,
        target: Target,
        name: str = "paged_kv_cache",
    ) -> None:
        """Create a paged KV cache object with FlashInfer kernels.

        Parameters
        ----------
        max_batch_size : tir.Var
            The maximum allowed batch size of the KV cache.
            It is a symbolic variable whose concrete value is specified
            at runtime.
        max_total_seq_len : tir.Var
            The maximum allowed total sequence length of the KV cache.
            It is a symbolic variable whose concrete value is specified
            at runtime.
        prefill_chunk_size : tir.Var
            The maximum total sequence length in a prefill.
            It is a symbolic variable whose concrete value is specified
            at runtime.
        page_size : tir.Var
            The size (a.k.a. number of tokens) of each page.
            It is a symbolic variable whose concrete value is specified
            at runtime.
        support_sliding_window : tir.Var
            0 or 1, denoting whether the KV cache supports sliding window.
            It is a symbolic variable whose concrete value is specified
            at runtime.
        layer_partition : rx.ShapeExpr
            The KV cache layer partition for pipeline stages.
            It is an indptr array, denoting the starting layer of each pipeline stage.
        rope_mode : RopeMode
            The RoPE mode of the Paged KV cache.
            If it is normal, RoPE will be applied to k before adding k to cache.
            Otherwise, RoPE will be applied to q/k in attention kernel on-the-fly.
        rope_scale : int
            The scale of rotary position embedding.
        rope_theta : int
            The base of rotary position embedding.
        rope_scaling: Dict[str, Any]
            The RoPE scaling information dict.
        rope_ext_factors: rx.Expr
            The RoPE extension factors when "longrope" mode RoPE scaling is enabled.
        rotary_dim : int
            The number of dimensions in the embedding that RoPE is applied to.
        enable_disaggregation : bool
            Whether to enable disaggregation in the KV cache.
        """
        if rope_mode == RopeMode.INLINE:
            assert rotary_dim == qk_head_dim, "FlashInfer RoPE does not support partial rotary dim."

        attn_kind_single = attn_kind[0] if isinstance(attn_kind, List) else attn_kind
        if attn_kind_single == "mha_sliding":
            attn_kind_single = "mha"
        flashinfer_prefill_mods = rx.backend.cuda.flashinfer.gen_flashinfer_prefill_module(
            dtype_q=dtype,
            dtype_kv=dtype,
            dtype_o=dtype,
            qk_head_dim=(qk_head_dim if attn_kind_single == "mha" else mla_original_qk_head_dim),
            v_head_dim=(v_head_dim if attn_kind_single == "mha" else mla_original_v_head_dim),
            target=target,
            enable_inline_rope=rope_mode == RopeMode.INLINE,
        )
        flashinfer_decode_mods = (
            rx.backend.cuda.flashinfer.gen_flashinfer_decode_module(
                dtype_q=dtype,
                dtype_kv=dtype,
                dtype_o=dtype,
                qk_head_dim=qk_head_dim,
                v_head_dim=v_head_dim,
                target=target,
            )
            if attn_kind_single == "mha"
            else []
        )
        flashinfer_mla_mods = (
            rx.backend.cuda.flashinfer.gen_flashinfer_mla_module(
                dtype_q=dtype,
                dtype_kv=dtype,
                dtype_o=dtype,
                head_dim_ckv=v_head_dim,
                head_dim_kpe=qk_head_dim - v_head_dim,
                target=target,
            )
            if attn_kind_single == "mla"
            else []
        )
        self.extern_mods = flashinfer_prefill_mods + flashinfer_decode_mods + flashinfer_mla_mods

        # fmt: off
        # pylint: disable=line-too-long
        bb = rx.BlockBuilder.current()
        mha_functions = (
            [
                rx.Tuple([rx.StringImm("flashinfer"), rx.ExternFunc("batch_prefill_with_paged_kv_cache_run"), rx.ExternFunc("batch_prefill_with_kv_cache_plan")]),
                rx.Tuple([rx.StringImm("flashinfer"), rx.ExternFunc("batch_decode_with_paged_kv_cache_run"), rx.ExternFunc("batch_decode_with_paged_kv_cache_plan")]),
                rx.Tuple([rx.StringImm("tir"), bb.add_func(_attention_prefill(num_key_value_heads, num_attention_heads, qk_head_dim, dtype, True, rope_scaling, target), "tir_attention_prefill_sliding_window")]),
                rx.Tuple([rx.StringImm("tir"), bb.add_func(_attention_decode(num_key_value_heads, num_attention_heads, qk_head_dim, dtype, True, rope_scaling, target), "tir_attention_decode_sliding_window")]),
                rx.Tuple([rx.StringImm("tir"), bb.add_func(tree_attn_with_paged_kv_cache(num_key_value_heads, num_attention_heads, qk_head_dim, dtype, rope_scaling, target), "tir_attention_prefill_with_tree_mask_with_paged_kv_cache")]),
                rx.Tuple([rx.StringImm("tir"), bb.add_func(tree_attn(num_key_value_heads, num_attention_heads, qk_head_dim, dtype, rope_scaling, target), "tir_attention_prefill_with_tree_mask")]),
            ]
            if attn_kind_single == "mha"
            else [rx.Tuple([]) for _ in range(6)]
        )
        mla_function = rx.Tuple([rx.StringImm("flashinfer"), rx.ExternFunc("batch_mla_paged_attention_run"), rx.ExternFunc("batch_mla_paged_attention_plan")] if attn_kind_single == "mla" else [])
        attn_merge_functions = [
            bb.add_func(_merge_state_inplace(num_attention_heads, v_head_dim, dtype, target, "tir_attention_merge_state"), "tir_attention_merge_state"),
        ]
        if attn_kind_single == "mla":
            attn_merge_functions.append(bb.add_func(_merge_state_inplace(num_attention_heads, mla_original_v_head_dim, dtype, target, "tir_attention_merge_state_mla"), "tir_attention_merge_state_mla"))

        if isinstance(attn_kind, List):
            attn_kind = [int(getattr(AttnKind, layer_kind.upper())) for layer_kind in attn_kind]
        else:
            attn_kind = [int(getattr(AttnKind, attn_kind.upper())) for _ in range(num_hidden_layers)]

        args = [
            rx.ShapeExpr(
                [
                    max_batch_size,
                    max_total_seq_len,
                    prefill_chunk_size,
                    page_size,
                    support_sliding_window,
                ]
            ),
            layer_partition,
            rx.PrimValue(num_attention_heads),
            rx.PrimValue(num_key_value_heads),
            rx.PrimValue(qk_head_dim),
            rx.PrimValue(v_head_dim),
            rx.ShapeExpr(attn_kind),
            rx.PrimValue(enable_disaggregation),
            rx.PrimValue(rope_mode),
            rx.PrimValue(rope_scale),
            rx.PrimValue(rope_theta),
            rope_ext_factors,
            rx.op.zeros((), dtype),
            bb.add_func(_kv_cache_transpose_append(num_key_value_heads, qk_head_dim, dtype), "kv_cache_transpose_append"),
            bb.add_func(_kv_cache_transpose_append_mla(qk_head_dim, dtype), "kv_cache_transpose_append_mla"),
            rx.Tuple([rx.StringImm("flashinfer"), rx.ExternFunc("batch_prefill_with_ragged_kv_cache_run"), rx.ExternFunc("batch_prefill_with_kv_cache_plan")]),
            *mha_functions,
            mla_function,
            rx.Tuple(attn_merge_functions),
            bb.add_func(llama_rope_with_position_map(rope_theta, rope_scale, qk_head_dim, num_attention_heads, num_key_value_heads, dtype, rope_scaling, rotary_dim), "tir_split_rotary"),
            bb.add_func(_copy_single_page(num_key_value_heads, page_size, qk_head_dim, dtype, target) if attn_kind_single == "mha" else _copy_single_page_mla(page_size, qk_head_dim, dtype, target), "kv_cache_copy_single_page"),
            bb.add_func(_kv_cache_debug_get_kv(num_hidden_layers, num_key_value_heads, qk_head_dim, dtype), "kv_cache_debug_get_kv"),
            bb.add_func(_compact_kv_copy(num_key_value_heads, qk_head_dim, dtype, target), "kv_cache_compact_kv_copy"),
            # fmt: on
            # pylint: enable=line-too-long
        ]
        super().__init__(
            _expr=rx.call_pure_packed(
                "vm.builtin.paged_attention_kv_cache_create",
                *args,
                sinfo_args=rx.ObjectStructInfo(),
            ),
            _name=name,
        )


class TIRPagedKVCache(PagedKVCache):  # pylint: disable=too-few-public-methods
    """Paged KV cache using TIR kernels."""

    def __init__(  # pylint: disable=too-many-locals
        self,
        attn_kind: Union[Literal["mha", "mla"], List[Literal["mha", "mla", "mha_sliding"]]],
        max_batch_size: tir.Var,
        max_total_seq_len: tir.Var,
        prefill_chunk_size: tir.Var,
        page_size: tir.Var,
        support_sliding_window: tir.Var,
        layer_partition: rx.ShapeExpr,
        num_hidden_layers: int,
        num_attention_heads: int,
        num_key_value_heads: int,
        qk_head_dim: int,
        v_head_dim: int,
        mla_original_qk_head_dim: int,
        mla_original_v_head_dim: int,
        rope_mode: RopeMode,
        rope_scale: int,
        rope_theta: int,
        rope_scaling: Dict[str, Any],
        rope_ext_factors: rx.Expr,
        rotary_dim: int,
        enable_disaggregation: bool,
        dtype: str,
        target: Target,
        name: str = "paged_kv_cache",
    ) -> None:
        """Create a paged KV cache object with TIR kernels.

        Parameters
        ----------
        max_batch_size : tir.Var
            The maximum allowed batch size of the KV cache.
            It is a symbolic variable whose concrete value is specified
            at runtime.
        max_total_seq_len : tir.Var
            The maximum allowed total sequence length of the KV cache.
            It is a symbolic variable whose concrete value is specified
            at runtime.
        prefill_chunk_size : tir.Var
            The maximum total sequence length in a prefill.
            It is a symbolic variable whose concrete value is specified
            at runtime.
        page_size : tir.Var
            The size (a.k.a. number of tokens) of each page.
            It is a symbolic variable whose concrete value is specified
            at runtime.
        support_sliding_window : tir.Var
            0 or 1, denoting whether the KV cache supports sliding window.
            It is a symbolic variable whose concrete value is specified
            at runtime.
        layer_partition : rx.ShapeExpr
            The KV cache layer partition for pipeline stages.
            It is an indptr array, denoting the starting layer of each pipeline stage.
        rope_mode : RopeMode
            The RoPE mode of the Paged KV cache.
            If it is normal, RoPE will be applied to k before adding k to cache.
            Otherwise, RoPE will be applied to q/k in attention kernel on-the-fly.
        rope_scale : int
            The scale of rotary position embedding.
        rope_theta : int
            The base of rotary position embedding.
        rope_scaling: Dict[str, Any]
            The RoPE scaling information dict.
        rope_ext_factors: rx.Expr
            The RoPE extension factors when "longrope" mode RoPE scaling is enabled.
        rotary_dim : int
            The number of dimensions in the embedding that RoPE is applied to.
        enable_disaggregation : bool
            Whether to enable disaggregation in the KV cache.
        target : Target
            The target to build the model to.
        """
        attn_kind_single = attn_kind[0] if isinstance(attn_kind, List) else attn_kind
        if attn_kind_single == "mha_sliding":
            attn_kind_single = "mha"
        if isinstance(attn_kind, List):
            attn_kind = [int(getattr(AttnKind, layer_kind.upper())) for layer_kind in attn_kind]
        else:
            attn_kind = [
                int(getattr(AttnKind, attn_kind.upper())) for _ in range(num_hidden_layers)
            ]
        bb = rx.BlockBuilder.current()
        args = [
            rx.ShapeExpr(
                [
                    max_batch_size,
                    max_total_seq_len,
                    prefill_chunk_size,
                    page_size,
                    support_sliding_window,
                ]
            ),
            layer_partition,
            rx.PrimValue(num_attention_heads),
            rx.PrimValue(num_key_value_heads),
            rx.PrimValue(qk_head_dim),
            rx.PrimValue(v_head_dim),
            rx.ShapeExpr(attn_kind),
            rx.PrimValue(enable_disaggregation),
            rx.PrimValue(rope_mode),
            rx.PrimValue(rope_scale),
            rx.PrimValue(rope_theta),
            rope_ext_factors,
            rx.op.zeros((), dtype),
            # pylint: disable=line-too-long
            # fmt: off
            bb.add_func(_kv_cache_transpose_append(num_key_value_heads, qk_head_dim, dtype), "kv_cache_transpose_append"),
            bb.add_func(_kv_cache_transpose_append_mla(qk_head_dim, dtype), "kv_cache_transpose_append_mla"),
            # fmt: on
            # pylint: enable=line-too-long
        ]

        if str(target.kind) == "llvm":
            if attn_kind_single == "mla":
                raise ValueError("MLA is not supported in TIR kernels for now.")
            # pylint: disable=line-too-long
            # fmt: off
            args.extend(
                [
                    rx.Tuple([rx.StringImm("tir"), bb.add_func(_attention_prefill_ragged_cpu(num_key_value_heads, num_attention_heads, qk_head_dim, v_head_dim, dtype, rope_scaling), "tir_attention_prefill_ragged_cpu")]),
                    rx.Tuple([rx.StringImm("tir"), bb.add_func(_attention_prefill_cpu(num_key_value_heads, num_attention_heads, qk_head_dim, dtype, False, rope_scaling), "tir_attention_prefill_cpu")]),
                    rx.Tuple([rx.StringImm("tir"), bb.add_func(_attention_decode_cpu(num_key_value_heads, num_attention_heads, qk_head_dim, dtype, False, rope_scaling), "tir_attention_decode_cpu")]),
                    rx.Tuple([rx.StringImm("tir"), bb.add_func(_attention_prefill_cpu(num_key_value_heads, num_attention_heads, qk_head_dim, dtype, True, rope_scaling), "tir_attention_prefill_cpu_sliding_window")]),
                    rx.Tuple([rx.StringImm("tir"), bb.add_func(_attention_decode_cpu(num_key_value_heads, num_attention_heads, qk_head_dim, dtype, True, rope_scaling), "tir_attention_decode_cpu_sliding_window")]),
                    rx.Tuple([rx.StringImm("tir"), bb.add_func(tree_attn_cpu(num_key_value_heads, num_attention_heads, qk_head_dim, dtype, rope_scaling), "tir_attention_prefill_with_tree_mask_cpu")]),
                    rx.Tuple([rx.StringImm("tir"), bb.add_func(tree_attn_with_paged_kv_cache_cpu(num_key_value_heads, num_attention_heads, qk_head_dim, dtype, rope_scaling), "tir_attention_prefill_with_tree_mask_with_paged_kv_cache_cpu")]),
                    rx.Tuple([]),  # f_mla_prefill
                    rx.Tuple([bb.add_func(_merge_state_inplace_cpu(dtype), "tir_attention_merge_state_cpu")]),
                    bb.add_func(llama_rope_with_position_map(rope_theta, rope_scale, qk_head_dim, num_attention_heads, num_key_value_heads, dtype, rope_scaling, rotary_dim), "tir_split_rotary"),
                    bb.add_func(_copy_single_page_cpu(num_key_value_heads, page_size, qk_head_dim, dtype), "kv_cache_copy_single_page_cpu"),
                    bb.add_func(_kv_cache_debug_get_kv(num_hidden_layers, num_key_value_heads, qk_head_dim, dtype), "kv_cache_debug_get_kv"),
                    bb.add_func(_compact_kv_copy_cpu(num_key_value_heads, qk_head_dim, dtype), "kv_cache_compact_kv_copy_cpu"),
                ]
            )
            # fmt: on
            # pylint: enable=line-too-long
        else:
            # pylint: disable=line-too-long
            # fmt: off
            ragged_qk_head_dim = qk_head_dim if attn_kind_single == "mha" else mla_original_qk_head_dim
            ragged_v_head_dim = v_head_dim if attn_kind_single == "mha" else mla_original_v_head_dim
            args.append(rx.Tuple([rx.StringImm("tir"), bb.add_func(_attention_prefill_ragged(num_key_value_heads if attn_kind_single == "mha" else num_attention_heads, num_attention_heads, ragged_qk_head_dim, ragged_v_head_dim, dtype, rope_scaling, target), "tir_attention_prefill_ragged")]))
            mha_functions = (
                [
                    rx.Tuple([rx.StringImm("tir"), bb.add_func(_attention_prefill(num_key_value_heads, num_attention_heads, qk_head_dim, dtype, False, rope_scaling, target), "tir_attention_prefill")]),
                    rx.Tuple([rx.StringImm("tir"), bb.add_func(_attention_decode(num_key_value_heads, num_attention_heads, qk_head_dim, dtype, False, rope_scaling, target), "tir_attention_decode")]),
                    rx.Tuple([rx.StringImm("tir"), bb.add_func(_attention_prefill(num_key_value_heads, num_attention_heads, qk_head_dim, dtype, True, rope_scaling, target), "tir_attention_prefill_sliding_window")]),
                    rx.Tuple([rx.StringImm("tir"), bb.add_func(_attention_decode(num_key_value_heads, num_attention_heads, qk_head_dim, dtype, True, rope_scaling, target), "tir_attention_decode_sliding_window")]),
                    rx.Tuple([rx.StringImm("tir"), bb.add_func(tree_attn_with_paged_kv_cache(num_key_value_heads, num_attention_heads, qk_head_dim, dtype, rope_scaling, target), "tir_attention_prefill_with_tree_mask_with_paged_kv_cache")]),
                    rx.Tuple([rx.StringImm("tir"), bb.add_func(tree_attn(num_key_value_heads, num_attention_heads, qk_head_dim, dtype, rope_scaling, target), "tir_attention_prefill_with_tree_mask")]),
                ]
                if attn_kind_single == "mha"
                else [rx.Tuple([]) for _ in range(6)]
            )
            mla_function = rx.Tuple([rx.StringImm("tir"), bb.add_func(_attention_prefill_mla(num_attention_heads, v_head_dim, qk_head_dim - v_head_dim, dtype, False, target), "tir_attention_prefill_mla")] if attn_kind_single == "mla" else [])
            attn_merge_functions = [
                bb.add_func(_merge_state_inplace(num_attention_heads, v_head_dim, dtype, target, "tir_attention_merge_state"), "tir_attention_merge_state"),
            ]
            if attn_kind_single == "mla":
                attn_merge_functions.append(bb.add_func(_merge_state_inplace(num_attention_heads, mla_original_v_head_dim, dtype, target, "tir_attention_merge_state_mla"), "tir_attention_merge_state_mla"))
            args.extend(mha_functions)
            args.append(mla_function)
            args.extend(
                [
                    rx.Tuple(attn_merge_functions),
                    bb.add_func(llama_rope_with_position_map(rope_theta, rope_scale, qk_head_dim, num_attention_heads, num_key_value_heads, dtype, rope_scaling, rotary_dim), "tir_split_rotary"),
                    bb.add_func(_copy_single_page(num_key_value_heads, page_size, qk_head_dim, dtype, target) if attn_kind_single == "mha" else _copy_single_page_mla(page_size, qk_head_dim, dtype, target), "kv_cache_copy_single_page"),
                    bb.add_func(_kv_cache_debug_get_kv(num_hidden_layers, num_key_value_heads, qk_head_dim, dtype), "kv_cache_debug_get_kv"),
                    bb.add_func(_compact_kv_copy(num_key_value_heads, qk_head_dim, dtype, target), "kv_cache_compact_kv_copy"),
                ]
            )
            # fmt: on
            # pylint: enable=line-too-long

        super().__init__(
            _expr=rx.call_pure_packed(
                "vm.builtin.paged_attention_kv_cache_create",
                *args,
                sinfo_args=rx.ObjectStructInfo(),
            ),
            _name=name,
        )


# mypy: disable-error-code="attr-defined,valid-type,no-redef"
# pylint: disable=too-many-locals


def _kv_cache_transpose_append(num_key_value_heads, head_dim, dtype, page_size: int = 16):
    """Return the TIR function that appends new k/v data to PagedKVCache."""

    # pylint: disable=line-too-long
    # fmt: off
    @T.prim_func
    def tir_kv_cache_transpose_append(
        var_pages: T.handle,
        var_k_data: T.handle,
        var_v_data: T.handle,
        var_position_map: T.handle,
    ):
        T.func_attr({"tir.noalias": True})
        ntoken = T.SizeVar("num_tokens_excluding_cache", "int64")
        num_pages = T.int64()
        pages_elem_offset = T.int64()
        position_map_elem_offset = T.int32()
        pages = T.match_buffer(var_pages, (num_pages, 2, num_key_value_heads, page_size, head_dim), dtype, elem_offset=pages_elem_offset)
        k_data = T.match_buffer(var_k_data, (ntoken, num_key_value_heads, head_dim), dtype)
        v_data = T.match_buffer(var_v_data, (ntoken, num_key_value_heads, head_dim), dtype)
        position_map = T.match_buffer(
            var_position_map, (ntoken,), "int32", elem_offset=position_map_elem_offset
        )
        for global_pos, h, f in T.grid(ntoken, num_key_value_heads, head_dim):
            if position_map[global_pos] != T.int32(-1):
                with T.block("k_transpose_append"):
                    vgpos, vh, vf = T.axis.remap("SSS", [global_pos, h, f])
                    T.reads(position_map[vgpos], k_data[vgpos, vh, vf])
                    T.writes(pages[position_map[vgpos] // page_size, 0, vh, position_map[vgpos] % page_size, vf])
                    position: T.int32 = position_map[vgpos]  # type: ignore
                    pages[T.floordiv(position, page_size), 0, vh, T.floormod(position, page_size), vf] = k_data[vgpos, vh, vf]
                with T.block("v_transpose_append"):
                    vgpos, vh, vf = T.axis.remap("SSS", [global_pos, h, f])
                    T.reads(position_map[vgpos], v_data[vgpos, vh, vf])
                    T.writes(pages[position_map[vgpos] // page_size, 1, vh, position_map[vgpos] % page_size, vf])
                    position: T.int32 = position_map[vgpos] # type: ignore[name-defined,no-redef]
                    pages[T.floordiv(position, page_size), 1, vh, T.floormod(position, page_size), vf] = v_data[vgpos, vh, vf]
    # fmt: on
    # pylint: enable=line-too-long

    return tir_kv_cache_transpose_append


def _kv_cache_transpose_append_mla(d_qk: int, dtype, page_size: int = 16):
    """Return the TIR function that appends new compressed KV data to PagedKVCache for MLA."""

    # pylint: disable=line-too-long
    # fmt: off
    @T.prim_func
    def tir_kv_cache_transpose_append_mla(
        var_pages: T.handle,
        var_kv_data: T.handle,
        var_position_map: T.handle,
    ):
        T.func_attr({"tir.noalias": True})
        ntoken = T.SizeVar("num_tokens_excluding_cache", "int64")
        num_pages = T.int64()
        pages_elem_offset = T.int64()
        position_map_elem_offset = T.int32()
        pages = T.match_buffer(var_pages, (num_pages, page_size, d_qk), dtype, elem_offset=pages_elem_offset)
        kv_data = T.match_buffer(var_kv_data, (ntoken, d_qk), dtype)
        position_map = T.match_buffer(
            var_position_map, (ntoken,), "int32", elem_offset=position_map_elem_offset
        )
        for global_pos, f in T.grid(ntoken, d_qk):
            if position_map[global_pos] != T.int32(-1):
                with T.block("k_transpose_append"):
                    vgpos, vf = T.axis.remap("SS", [global_pos, f])
                    T.reads(position_map[vgpos], kv_data[vgpos, vf])
                    T.writes(pages[position_map[vgpos] // page_size, position_map[vgpos] % page_size, vf])
                    position: T.int32 = position_map[vgpos]  # type: ignore
                    pages[T.floordiv(position, page_size), T.floormod(position, page_size), vf] = kv_data[vgpos, vf]
    # fmt: on
    # pylint: enable=line-too-long

    return tir_kv_cache_transpose_append_mla


def _kv_cache_debug_get_kv(num_hidden_layers, num_key_value_heads, head_dim, dtype):
    """Return the TIR function that fetches the k/v data on given positions and layer."""

    # pylint: disable=line-too-long
    # fmt: off
    @T.prim_func
    def tir_kv_cache_debug_get_kv(
        var_pages: T.handle,
        var_position_map: T.handle,
        var_k_data: T.handle,
        var_v_data: T.handle,
        layer_id: T.int64,
    ):
        T.func_attr({"tir.noalias": True})
        seqlen = T.SizeVar("num_tokens_including_cache", "int64")
        page_size = T.SizeVar("page_size", "int64")
        num_pages = T.int64()
        pages_elem_offset = T.int64()
        position_map_elem_offset = T.int64()
        pages = T.match_buffer(var_pages, (num_pages, 2, num_key_value_heads, page_size, head_dim), dtype,elem_offset=pages_elem_offset)
        position_map = T.match_buffer(
            var_position_map, (seqlen,), "int32", elem_offset=position_map_elem_offset
        )
        k_data = T.match_buffer(var_k_data, (num_hidden_layers, seqlen, num_key_value_heads, head_dim), dtype)
        v_data = T.match_buffer(var_v_data, (num_hidden_layers, seqlen, num_key_value_heads, head_dim), dtype)
        for p, h, d in T.grid(seqlen, num_key_value_heads, head_dim):
            with T.block("copy0"):
                vp, vh, vd = T.axis.remap("SSS", [p, h, d])
                T.reads(position_map[vp], pages[position_map[vp] // page_size, 0:2, vh, position_map[vp] % page_size, vd])
                T.writes(k_data[layer_id, vp, vh, vd], v_data[layer_id, vp, vh, vd])
                position: T.int32 = position_map[vp] # type: ignore[name-defined]
                k_data[layer_id, vp, vh, vd] = pages[T.floordiv(position, page_size), 0, vh, T.floormod(position, page_size), vd]
                v_data[layer_id, vp, vh, vd] = pages[T.floordiv(position, page_size), 1, vh, T.floormod(position, page_size), vd]
    # fmt: on
    # pylint: enable=line-too-long

    return tir_kv_cache_debug_get_kv


def _kv_cache_debug_get_kv_mla(num_hidden_layers, d_qk, dtype):
    """Return the TIR function that fetches the k/v data on given positions and layer."""

    # pylint: disable=line-too-long
    # fmt: off
    @T.prim_func
    def tir_kv_cache_debug_get_kv_mla(
        var_pages: T.handle,
        var_position_map: T.handle,
        var_compressed_kv_with_k_pe_data: T.handle,
        layer_id: T.int64,
    ):
        T.func_attr({"tir.noalias": True})
        seqlen = T.SizeVar("num_tokens_including_cache", "int64")
        page_size = T.SizeVar("page_size", "int64")
        num_pages = T.int64()
        pages_elem_offset = T.int64()
        position_map_elem_offset = T.int64()
        pages = T.match_buffer(var_pages, (num_pages, page_size, d_qk), dtype, elem_offset=pages_elem_offset)
        position_map = T.match_buffer(
            var_position_map, (seqlen,), "int32", elem_offset=position_map_elem_offset
        )
        compressed_kv_with_k_pe_data = T.match_buffer(var_compressed_kv_with_k_pe_data, (num_hidden_layers, seqlen, d_qk), dtype)
        for p, d in T.grid(seqlen, d_qk):
            with T.block("copy0"):
                vp, vd = T.axis.remap("SS", [p, d])
                T.reads(position_map[vp], pages[position_map[vp] // page_size, position_map[vp] % page_size, vd])
                T.writes(compressed_kv_with_k_pe_data[layer_id, vp, vd])
                position: T.int32 = position_map[vp] # type: ignore[name-defined]
                compressed_kv_with_k_pe_data[layer_id, vp, vd] = pages[T.floordiv(position, page_size), T.floormod(position, page_size), vd]
    # fmt: on
    # pylint: enable=line-too-long

    return tir_kv_cache_debug_get_kv_mla


def _rope(
    buffer: T.Buffer,
    offset: tir.Var,
    rotary_dim: int,
    theta: tir.Var,
    scale: tir.Var,
    indices: Tuple[tir.Var, ...],
    qkv_dtype: str,
    rope_scaling: Dict[str, Any],
):
    d = indices[-1]
    cos_freq, sin_freq, var_map = switch_rope_freq_func(rope_scaling)(
        offset * scale, d, rotary_dim, theta, "float32"
    )
    cos = cos_freq * buffer[indices].astype("float32")
    sin = sin_freq * tir.if_then_else(
        d < rotary_dim // 2,
        -buffer[indices[:-1] + (d + rotary_dim // 2,)],
        buffer[indices[:-1] + (d - rotary_dim // 2,)],
    ).astype("float32")
    expr = (cos + sin).astype(qkv_dtype)
    for var, value in var_map.items():
        expr = tir.Let(var, value, expr)
    return expr


def _var(dtype):
    return T.alloc_buffer((1,), dtype, scope="local")


def _causal_mask(causal, row, col, kv_len, qo_len):
    return T.if_then_else(
        causal > 0,
        col < kv_len - qo_len + row + 1,
        col < kv_len,
    )


def _declare_length_info(var_length_info, batch_size, sliding_window, elem_offset):
    return (
        T.match_buffer(var_length_info, (3, batch_size), "int32", elem_offset=elem_offset)
        if sliding_window
        else T.match_buffer(var_length_info, (batch_size,), "int32", elem_offset=elem_offset)
    )


def _get_kv_chunk_len(num_pages, page_size, seq_id, length_info, sliding_window):
    if not sliding_window:
        return (num_pages - 1) * page_size + length_info[seq_id]
    # ((num_pages - 1) * page_size + last_page_len) - sliding_window_offset + sink_size
    return (
        (num_pages - 1) * page_size
        + length_info[0, seq_id]
        - length_info[1, seq_id]
        + length_info[2, seq_id]
    )


def _get_seq_offset(pos, seq_id, length_info, sliding_window):
    if not sliding_window:
        return pos
    # pos if pos < sink_size else pos - sink_size + sliding_window_offset
    return T.if_then_else(
        pos < length_info[2, seq_id],
        pos,
        pos - length_info[2, seq_id] + length_info[1, seq_id],
    )


def _attention_prefill_cpu(
    h_kv, h_q, d, dtype, sliding_window: bool, rope_scaling: Dict[str, Any], page_size: int = 16
):
    global_symbol = "batch_prefill_paged_kv_cpu"
    if sliding_window:
        global_symbol += "_sliding_window"

    group_size = h_q // h_kv
    # pylint: disable=line-too-long,too-many-branches
    # fmt: off
    @T.prim_func
    def batch_prefill_paged_kv_cpu(
        var_q: T.handle, # [total_len, h_q, d]
        var_q_indptr: T.handle, # [batch_size + 1]
        var_pages: T.handle, # [max_num_pages, 2, h_kv, page_size, d]
        var_page_indptr: T.handle, # [batch_size + 1]
        var_page_values: T.handle, # [nnz_pages]
        var_length_info: T.handle, # [b] when sliding window = False, or otherwise [3, b]
        var_k_rope_pos_offset: T.handle, # [b]
        var_q_rope_position: T.handle, # [total_len]
        var_output: T.handle, # [total_len, h_q, d]
        var_lse: T.handle, # [total_len, h_q]
        causal: T.int32,
        rotary_mode: T.int32,
        rope_scale: T.float32,
        rope_theta: T.float32,
        sm_scale: T.float32,
    ):
        T.func_attr({"global_symbol": global_symbol})
        batch_size = T.int32(is_size_var=True)
        total_len = T.int32(is_size_var=True)
        nnz_pages = T.int32(is_size_var=True)
        max_num_pages = T.int32(is_size_var=True)
        q_indptr_elem_offset = T.int32(is_size_var=True)
        page_indptr_elem_offset = T.int32(is_size_var=True)
        page_values_elem_offset = T.int32(is_size_var=True)
        k_rope_pos_offset_elem_offset = T.int32(is_size_var=True)
        q_rope_position_elem_offset = T.int32(is_size_var=True)
        length_info_elem_offset = T.int32(is_size_var=True)

        q = T.match_buffer(var_q, (total_len, h_q, d), dtype)
        q_indptr = T.match_buffer(var_q_indptr, (batch_size + 1,), "int32", elem_offset=q_indptr_elem_offset)
        pages = T.match_buffer(var_pages, (max_num_pages, 2, h_kv, page_size, d), dtype)
        page_indptr = T.match_buffer(var_page_indptr, (batch_size + 1,), "int32", elem_offset=page_indptr_elem_offset)
        page_values = T.match_buffer(var_page_values, (nnz_pages,), "int32", elem_offset=page_values_elem_offset)
        k_rope_pos_offset = T.match_buffer(var_k_rope_pos_offset, (batch_size,), "int32", elem_offset=k_rope_pos_offset_elem_offset)
        q_rope_position = T.match_buffer(var_q_rope_position, (total_len,), "int32", elem_offset=q_rope_position_elem_offset)
        output = T.match_buffer(var_output, (total_len, h_q, d), dtype)
        lse = T.match_buffer(var_lse, (total_len, h_q), "float32")  # pylint: disable=unused-variable
        # The length information of the sequences.
        # - It is in shape `(3, batch_size)` when sliding window is enabled.
        #   For a sequence "i", location
        #   - "(0, i)" is the number of KV slots used in the last page of the seq ("last_page_len"),
        #   - "(1, i)" is the starting offset of the sliding window in the seq,
        #   - "(2, i)" is the attn sink length of the sequence.
        # - It is in shape `(batch_size,)` when sliding window is disabled,
        #   denoting the "last_page_len".
        length_info = _declare_length_info(var_length_info, batch_size, sliding_window, length_info_elem_offset)


        for h_qo in T.serial(h_q):
            for b_idx in T.serial(batch_size):
                with T.block("attn"):
                    O_local = T.alloc_buffer((d, ), "float32")
                    Q_local = T.alloc_buffer((d, ), "float32")
                    K_local = T.alloc_buffer((d, ), "float32")
                    V_local = T.alloc_buffer((d, ), "float32")

                    kv_chunk_len = T.alloc_buffer((1, ), "int32")

                    m_val = T.alloc_buffer((1, ), "float32")
                    new_m = T.alloc_buffer((1, ), "float32")
                    d_val = T.alloc_buffer((1, ), "float32")
                    S_val = T.alloc_buffer((1, ), "float32")
                    scale_O = T.alloc_buffer((1, ), "float32")
                    factor = T.alloc_buffer((1, ), "float32")
                    cur_page_indptr_begin: T.int32 = page_indptr[b_idx]
                    cur_page_indptr_end: T.int32 = page_indptr[b_idx + 1]
                    #max_kv_len: T.int32 = max_num_pages * page_size
                    kv_chunk_len[0] = T.if_then_else(
                        cur_page_indptr_begin != cur_page_indptr_end,
                        _get_kv_chunk_len(cur_page_indptr_end - cur_page_indptr_begin, page_size, b_idx, length_info, sliding_window),
                        0
                    )


                    for q_idx in T.serial(q_indptr[b_idx + 1] - q_indptr[b_idx]):
                        #init m, d, O
                        m_val[0] = -5e4
                        d_val[0] = 1.0
                        for d_idx in T.serial(d):
                            O_local[d_idx] = 0.0
                        curl_q: T.int32 = q_indptr[b_idx] + q_idx

                        for d_idx in T.serial(d):

                            Q_local[d_idx] = T.if_then_else(
                                rotary_mode == 1,
                                _rope(q, q_rope_position[curl_q], d, rope_theta, rope_scale, (curl_q, h_qo, d_idx), dtype, rope_scaling),
                                q[curl_q, h_qo, d_idx]
                            )
                        for row_idx in T.serial(max_num_pages * page_size):
                            if row_idx < kv_chunk_len[0]:
                                # seq_offset: T.int32(is_size_var=True) = _get_seq_offset(row_idx, b_idx, length_info, sliding_window)
                                #seq_offset: T.int32(is_size_var=True) = row_idx
                                page_no: T.int32(is_size_var=True) = page_values[cur_page_indptr_begin + (_get_seq_offset(row_idx, b_idx, length_info, sliding_window) // page_size)]
                                page_offset: T.int32(is_size_var=True) = _get_seq_offset(row_idx, b_idx, length_info, sliding_window) % page_size

                                # Load KV
                                for d_idx in T.serial(d):
                                    K_local[d_idx] = T.if_then_else(
                                        rotary_mode == 1,
                                        _rope(pages, k_rope_pos_offset[b_idx] + row_idx, d, rope_theta, rope_scale, (page_no, 0, h_qo // group_size, page_offset, d_idx), dtype, rope_scaling),
                                        pages[page_no, 0, h_qo // group_size, page_offset, d_idx]
                                    )
                                    V_local[d_idx] = pages[page_no, 1, h_qo // group_size, page_offset, d_idx]

                                # Compute S
                                # Q[i] * K[i] * sm_scale
                                S_val[0] = 0.0
                                for d_idx in T.serial(d):
                                    S_val[0] += Q_local[d_idx] * K_local[d_idx]
                                S_val[0] *= sm_scale * math.log2(math.exp(1))

                                # update m_val, d_val , O_local
                                if _causal_mask(causal,
                                    row=q_idx,
                                    col=row_idx,
                                    kv_len=kv_chunk_len[0],
                                    qo_len=q_indptr[b_idx + 1] - q_indptr[b_idx]):
                                    new_m[0] = T.max(m_val[0], S_val[0])
                                else:
                                    S_val[0] = -5e4
                                # update d_val
                                d_val[0] *= T.exp2(m_val[0] - new_m[0])
                                d_val[0] += T.exp2(S_val[0] - new_m[0])

                                # restore O_local then update O_local
                                scale_O[0] = T.exp2(m_val[0] - new_m[0])
                                m_val[0] = new_m[0]
                                factor[0] = T.exp2(S_val[0] - m_val[0])
                                for d_idx in T.serial(d):
                                    O_local[d_idx] = O_local[d_idx] * scale_O[d_idx]


                                for d_idx in T.serial(d):
                                    O_local[d_idx] += V_local[d_idx] * factor[0]
                        # Store Output
                        for d_idx in T.serial(d):
                            O_local[d_idx] = O_local[d_idx] /d_val[0]
                            output[curl_q, h_qo, d_idx] = O_local[d_idx]
                        lse[curl_q, h_qo] = m_val[0] + T.log2(d_val[0])
    return batch_prefill_paged_kv_cpu


def _get_prefill_kernel_config(h_kv, h_q, d, dtype, target: Target):
    NUM_BLKS = 16
    LOAD_VEC = 8 // ((DataType(dtype).bits + 7) // 8)  # 8 bytes
    group_size = h_q // h_kv

    bdx = 32
    num_warps = 4
    tile_x, tile_y, tile_z = (
        64 // ((DataType(dtype).bits + 7) // 8) // max(d // 128, 1),
        d,
        64 // ((DataType(dtype).bits + 7) // 8) // max(d // 128, 1),
    )
    original_tile_y = tile_y
    original_tile_z = tile_z
    while (tile_x * tile_z) % (bdx * num_warps) != 0:
        tile_z += original_tile_z
    while (tile_x * tile_y) % (bdx * num_warps) != 0:
        tile_y += original_tile_y

    # Otherwise we would exceed maxComputeWorkgroupStorageSize
    if (
        str(target.kind) == "webgpu"
        and ((d + 127) // 128) * ((DataType(dtype).bits + 15) // 16) >= 4
    ):
        tile_z = 8
        num_warps = 2
    if target.kind.name == "opencl" and (
        ("android" in str(target.host)) or ("adreno" in str(target.attrs))
    ):
        LOAD_VEC = 16 // ((DataType(dtype).bits + 7) // 8)  # 16 bytes
        NUM_BLKS = group_size * 8

    check_thread_limits(target, bdx=bdx, bdy=num_warps, bdz=1, gdz=1)

    return NUM_BLKS, LOAD_VEC, group_size, bdx, num_warps, tile_x, tile_y, tile_z


def _schedule_prefill_kernel(
    sch: tir.Schedule,
    load_vec,
    bdx,
    num_warps,
    tile_x,
    tile_y,
    tile_z,
    transform_k_load: bool,
    merged_qk_load: bool,
) -> tir.Schedule:
    get_extent = lambda *lps: [int(sch.get(lp).extent) for lp in lps]

    def get_vecsize(extent):
        return min(load_vec, (extent & ~(extent - 1)))

    def getxy_vecsize(x, y, t):
        assert (x * y) % t == 0
        return min(get_vecsize(y), get_vecsize(x * y // t))

    def get_tile_size(x, y, t):
        cnt = (x * y) // t
        assert (x * y) % t == 0
        tile_y = (int)(math.ceil(math.sqrt(cnt)))
        while (cnt % tile_y != 0 or y % tile_y != 0 or x % (cnt // tile_y) != 0) and tile_y <= cnt:
            tile_y += 1
        assert tile_y <= cnt
        tile_x = cnt // tile_y
        return tile_x, tile_y

    def apply_to_qkv_load(sch: tir.Schedule, block):
        loop_x, loop_y = sch.get_loops(block)[-2:]
        x_extent, y_extent = get_extent(loop_x, loop_y)
        vec_size = getxy_vecsize(x_extent, y_extent, bdx * num_warps)
        yo, yv = sch.split(loop_y, [None, vec_size])
        yo_extent = y_extent // vec_size
        tile_x, tile_y = get_tile_size(x_extent, yo_extent, (bdx * num_warps))
        xo, xi = sch.split(loop_x, [tile_x, None])
        yo, yi = sch.split(yo, [tile_y, None])
        sch.reorder(xi, yi, xo, yo)
        t = sch.fuse(xi, yi)
        ty, tx = sch.split(t, [num_warps, bdx])
        sch.bind(ty, "threadIdx.y")
        sch.bind(tx, "threadIdx.x")
        sch.vectorize(yv)

    def apply_to_so_ewise(sch: tir.Schedule, block, tile):
        loop_x, loop_y = sch.get_loops(block)[-2:]
        xo, xi = sch.split(loop_x, factors=[None, tile[0]])
        yo, yi = sch.split(loop_y, factors=[None, tile[1]])
        sch.reorder(xo, yo, xi, yi)
        yiv_extent = get_vecsize(tile[1])
        yio, yiv = sch.split(yi, [None, yiv_extent])
        sch.unroll(yio)
        sch.vectorize(yiv)
        t = sch.fuse(xo, yo)
        ty, tx = sch.split(t, factors=[None, bdx])
        sch.bind(ty, "threadIdx.y")
        sch.bind(tx, "threadIdx.x")

    def apply_to_gemm(sch: tir.Schedule, block, tile, r_len=16, k_major=False):
        loop_x, loop_y, loop_z = sch.get_loops(block)[-3:]
        xo, xi = sch.split(loop_x, factors=[None, tile[0]])
        yo, yi = sch.split(loop_y, factors=[None, tile[1]])
        sch.reorder(xo, yo, xi, yi)
        t = sch.fuse(xo, yo)
        ty, tx = sch.split(t, factors=[None, bdx])
        sch.bind(ty, "threadIdx.y")
        sch.bind(tx, "threadIdx.x")

        ko, ki = sch.split(loop_z, factors=[None, r_len])
        if k_major:
            sch.reorder(ko, xi, yi, ki)
        else:
            sch.reorder(ko, ki, xi, yi)
        yiv_extent = get_vecsize(tile[1])
        yio, yiv = sch.split(yi, [None, yiv_extent])
        sch.unroll(yio)
        sch.vectorize(yiv)
        sch.unroll(xi)
        sch.decompose_reduction(block, ty)

    def apply_to_md(sch, block):
        loop = sch.get_loops(block)[-1]
        _, ty, tx = sch.split(loop, factors=[None, num_warps, bdx])
        sch.bind(ty, "threadIdx.y")
        sch.bind(tx, "threadIdx.x")

    if transform_k_load and not merged_qk_load:
        sch.transform_layout("K_load", ("write", 0), lambda i, j: (j, i))
    tile_s = get_tile_size(tile_x, tile_z, bdx * num_warps)
    tile_o = get_tile_size(tile_x, tile_y, bdx * num_warps)
    apply_to_gemm(sch, sch.get_block("S_gemm"), tile_s, k_major=True)
    apply_to_gemm(sch, sch.get_block("O_gemm"), tile_o, k_major=False)
    apply_to_so_ewise(sch, sch.get_block("S_store"), tile_s)
    apply_to_so_ewise(sch, sch.get_block("O_init"), tile_o)
    apply_to_so_ewise(sch, sch.get_block("O_store"), tile_o)
    apply_to_qkv_load(sch, sch.get_block("Q_load"))
    if not merged_qk_load:
        apply_to_qkv_load(sch, sch.get_block("K_load"))
        apply_to_qkv_load(sch, sch.get_block("V_load"))
    else:
        apply_to_qkv_load(sch, sch.get_block("KV_load"))
    apply_to_md(sch, sch.get_block("lse_store"))
    return sch


def _attention_prefill(
    h_kv,
    h_q,
    d,
    dtype,
    sliding_window: bool,
    rope_scaling: Dict[str, Any],
    target: Target,
    page_size: int = 16,
):
    (
        NUM_BLKS,
        LOAD_VEC,
        group_size,
        bdx,
        num_warps,
        tile_x,
        tile_y,
        tile_z,
    ) = _get_prefill_kernel_config(h_kv, h_q, d, dtype, target)

    global_symbol = "batch_prefill_paged_kv"
    if sliding_window:
        global_symbol += "_sliding_window"

    # pylint: disable=line-too-long,too-many-branches
    # fmt: off
    @T.prim_func
    def batch_prefill_paged_kv(
        var_q: T.handle, # [total_len, h_q, d]
        var_q_indptr: T.handle, # [batch_size + 1]
        var_pages: T.handle, # [max_num_pages, 2, h_kv, page_size, d]
        var_page_indptr: T.handle, # [batch_size + 1]
        var_page_values: T.handle, # [nnz_pages]
        var_length_info: T.handle, # [b] when sliding window = False, or otherwise [3, b]
        var_k_rope_pos_offset: T.handle, # [b]
        var_q_rope_position: T.handle, # [total_len]
        var_output: T.handle, # [total_len, h_q, d]
        var_lse: T.handle, # [total_len, h_q]
        causal: T.int32,
        rotary_mode: T.int32,
        rope_scale: T.float32,
        rope_theta: T.float32,
        sm_scale: T.float32,
    ):
        T.func_attr({"global_symbol": global_symbol})
        batch_size = T.int32(is_size_var=True)
        total_len = T.int32(is_size_var=True)
        nnz_pages = T.int32(is_size_var=True)
        max_num_pages = T.int32(is_size_var=True)
        pages_elem_offset = T.int64(is_size_var=True)
        q_indptr_elem_offset = T.int32(is_size_var=True)
        page_indptr_elem_offset = T.int32(is_size_var=True)
        page_values_elem_offset = T.int32(is_size_var=True)
        k_rope_pos_offset_elem_offset = T.int32(is_size_var=True)
        q_rope_position_elem_offset = T.int32(is_size_var=True)
        length_info_elem_offset = T.int32(is_size_var=True)

        q = T.match_buffer(var_q, (total_len, h_q, d), dtype)
        q_indptr = T.match_buffer(var_q_indptr, (batch_size + 1,), "int32", elem_offset=q_indptr_elem_offset)
        pages = T.match_buffer(var_pages, (max_num_pages, 2, h_kv, page_size, d), dtype, elem_offset=pages_elem_offset)
        page_indptr = T.match_buffer(var_page_indptr, (batch_size + 1,), "int32", elem_offset=page_indptr_elem_offset)
        page_values = T.match_buffer(var_page_values, (nnz_pages,), "int32", elem_offset=page_values_elem_offset)
        k_rope_pos_offset = T.match_buffer(var_k_rope_pos_offset, (batch_size,), "int32", elem_offset=k_rope_pos_offset_elem_offset)
        q_rope_position = T.match_buffer(var_q_rope_position, (total_len,), "int32", elem_offset=q_rope_position_elem_offset)
        output = T.match_buffer(var_output, (total_len, h_q, d), dtype)
        lse = T.match_buffer(var_lse, (total_len, h_q), "float32")  # pylint: disable=unused-variable
        # The length information of the sequences.
        # - It is in shape `(3, batch_size)` when sliding window is enabled.
        #   For a sequence "i", location
        #   - "(0, i)" is the number of KV slots used in the last page of the seq ("last_page_len"),
        #   - "(1, i)" is the starting offset of the sliding window in the seq,
        #   - "(2, i)" is the attn sink length of the sequence.
        # - It is in shape `(batch_size,)` when sliding window is disabled,
        #   denoting the "last_page_len".
        length_info = _declare_length_info(var_length_info, batch_size, sliding_window, length_info_elem_offset)

        # kernel code
        for lbx in T.thread_binding(NUM_BLKS, thread="blockIdx.x"):
            for lby in T.thread_binding(h_kv, thread="blockIdx.y"):
                for lty in T.thread_binding(num_warps, thread="threadIdx.y"):
                    for ltx in T.thread_binding(bdx, thread="threadIdx.x"):
                        with T.block("attn"):
                            bx, by, ty, tx = T.axis.remap("SSSS", [lbx, lby, lty, ltx])
                            T.reads()
                            T.writes()
                            tile_id = _var("int32")
                            batch_idx = _var("int32")
                            batch_tiles = _var("int32")
                            batch_rows = _var("int32")
                            iterator = _var("int32")
                            kv_chunk_len = _var("int32")

                            Q_smem = T.alloc_buffer((tile_x, d), dtype, scope="shared")
                            K_smem = T.alloc_buffer((tile_z, d), dtype, scope="shared")
                            V_smem = T.alloc_buffer((tile_z, d), dtype, scope="shared")
                            S_smem = T.alloc_buffer((tile_x, tile_z), "float32", scope="shared")

                            S_local = T.alloc_buffer((tile_x, tile_z), "float32", scope="local")
                            O_local = T.alloc_buffer((tile_x, d), "float32", scope="local")

                            m_smem = T.alloc_buffer((tile_x, ), "float32", scope="shared")
                            m_prev_smem = T.alloc_buffer((tile_x, ), "float32", scope="shared")
                            d_smem = T.alloc_buffer((tile_x, ), "float32", scope="shared")

                            m_new = T.alloc_buffer((math.ceil(tile_x / (bdx * num_warps)),), "float32", scope="local")
                            m_prev = T.alloc_buffer((math.ceil(tile_x / (bdx * num_warps)),), "float32", scope="local")
                            d_new = T.alloc_buffer((math.ceil(tile_x / (bdx * num_warps)),), "float32", scope="local")

                            ## get tile_no, batch_idx, batch_tiles, batch_rows
                            tile_id[0] = bx
                            batch_idx[0] = 0
                            batch_rows[0] = (q_indptr[1] - q_indptr[0]) * group_size
                            batch_tiles[0] = T.ceildiv(batch_rows[0], tile_x)
                            while T.tvm_thread_invariant(batch_idx[0] < batch_size):
                                # advance to next tile
                                while tile_id[0] >= batch_tiles[0] and batch_idx[0] < batch_size:
                                    tile_id[0] -= batch_tiles[0]
                                    batch_idx[0] += 1
                                    if batch_idx[0] < batch_size:
                                        b_idx: T.int32 = batch_idx[0]
                                        batch_rows[0] = (q_indptr[b_idx + 1] - q_indptr[b_idx]) * group_size
                                        batch_tiles[0] = T.ceildiv(batch_rows[0], tile_x)

                                if T.tvm_thread_invariant(batch_idx[0] < batch_size):
                                    b_idx: T.int32 = batch_idx[0]
                                    LH_start: T.int32 = tile_id[0] * tile_x
                                    q_indptr_val: T.int32 = q_indptr[b_idx]

                                    cur_page_indptr_begin: T.int32 = page_indptr[b_idx]
                                    cur_page_indptr_end: T.int32 = page_indptr[b_idx + 1]
                                    kv_chunk_len[0] = T.if_then_else(
                                        cur_page_indptr_begin != cur_page_indptr_end,
                                        _get_kv_chunk_len(cur_page_indptr_end - cur_page_indptr_begin, page_size, b_idx, length_info, sliding_window),
                                        0
                                    )
                                    T.tvm_storage_sync("shared")

                                    # init states
                                    for i in T.serial(T.ceildiv(tile_x, bdx * num_warps)):
                                        row: T.int32 = i * bdx * num_warps + ty * bdx + tx
                                        if row < tile_x:
                                            m_smem[row] = -5e4
                                            d_smem[row] = 1.0

                                    for li, lj in T.grid(tile_x, tile_y):
                                        with T.block("O_init"):
                                            i, j = T.axis.remap("SS", [li, lj])
                                            O_local[i, j] = 0.0
                                    T.tvm_storage_sync("shared")

                                    # Load Q from gmem to smem
                                    for li, lj in T.grid(tile_x, tile_y):
                                        with T.block("Q_load"):
                                            i, j = T.axis.remap("SS", [li, lj])
                                            T.reads()
                                            T.writes()
                                            cur_L = q_indptr_val + (LH_start + i) // group_size
                                            cur_H_qo = by * group_size + (LH_start + i) % group_size
                                            if cur_L < q_indptr[b_idx + 1]:
                                                Q_smem[i, j] = T.if_then_else(
                                                    rotary_mode == 1,
                                                    _rope(q, q_rope_position[cur_L], d, rope_theta, rope_scale, (cur_L, cur_H_qo, j), dtype, rope_scaling),
                                                    q[cur_L, cur_H_qo, j]
                                                )
                                            else:
                                                Q_smem[i, j] = 0.0
                                    T.tvm_storage_sync("shared")

                                    for iterator in T.serial(T.ceildiv(kv_chunk_len[0], tile_z)):
                                        L_kv_start: T.int32 = iterator * tile_z
                                        for lz, ly in T.grid(tile_z, tile_y):
                                            with T.block("K_load"):
                                                i, j = T.axis.remap("SS", [lz, ly])
                                                T.reads()
                                                T.writes()
                                                cur_L = L_kv_start + i
                                                if cur_L < kv_chunk_len[0]:
                                                    seq_offset: T.int32(is_size_var=True) = _get_seq_offset(cur_L, b_idx, length_info, sliding_window)  # type: ignore
                                                    page_no: T.int32(is_size_var=True) = page_values[cur_page_indptr_begin + T.floordiv(seq_offset, page_size)]  # type: ignore
                                                    page_offset: T.int32(is_size_var=True) = T.floormod(seq_offset, page_size)  # type: ignore
                                                    K_smem[i, j] = T.if_then_else(
                                                        rotary_mode == 1,
                                                        _rope(pages, k_rope_pos_offset[b_idx] + cur_L, d, rope_theta, rope_scale, (page_no, 0, by, page_offset, j), dtype, rope_scaling),
                                                        pages[page_no, 0, by, page_offset, j]
                                                    )
                                                else:
                                                    K_smem[i, j] = 0.0
                                        T.tvm_storage_sync("shared")
                                        for lz, ly in T.grid(tile_z, tile_y):
                                            with T.block("V_load"):
                                                i, j = T.axis.remap("SS", [lz, ly])
                                                T.reads()
                                                T.writes()
                                                cur_L = L_kv_start + i
                                                if cur_L < kv_chunk_len[0]:
                                                    seq_offset: T.int32(is_size_var=True) = _get_seq_offset(cur_L, b_idx, length_info, sliding_window)  # type: ignore
                                                    page_no: T.int32(is_size_var=True) = page_values[cur_page_indptr_begin + T.floordiv(seq_offset, page_size)]  # type: ignore
                                                    page_offset: T.int32(is_size_var=True) = T.floormod(seq_offset, page_size)  # type: ignore
                                                    V_smem[i, j] = pages[page_no, 1, by, page_offset, j]
                                                else:
                                                    V_smem[i, j] = 0.0
                                        T.tvm_storage_sync("shared")

                                        # Compute S
                                        with T.block():
                                            for li, lj, lk in T.grid(tile_x, tile_z, tile_y):
                                                with T.block("S_gemm"):
                                                    i, j, k = T.axis.remap("SSR", [li, lj, lk])
                                                    with T.init():
                                                        S_local[i, j] = 0.0
                                                    S_local[i, j] += T.cast(Q_smem[i, k], "float32") * T.cast(K_smem[j, k], "float32") * sm_scale * math.log2(math.exp(1))
                                        T.tvm_storage_sync("shared")
                                        for li, lj in T.grid(tile_x, tile_z):
                                            with T.block("S_store"):
                                                i, j = T.axis.remap("SS", [li, lj])
                                                S_smem[i, j] = S_local[i, j]
                                        T.tvm_storage_sync("shared")

                                        # Update S, m, d
                                        for i in T.serial(T.ceildiv(tile_x, bdx * num_warps)):
                                            row: T.int32 = i * bdx * num_warps + ty * bdx + tx
                                            if row < tile_x:
                                                with T.block("update1"):
                                                    m_prev[i] = m_smem[row]
                                                    m_new[i] = m_smem[row]
                                                    # mask out of kv_chunk_len S
                                                    row_: T.int32 = (LH_start + row) // group_size
                                                    for j in T.serial(tile_z):
                                                        if _causal_mask(causal,
                                                                row=row_,
                                                                col=L_kv_start + j,
                                                                kv_len=kv_chunk_len[0],
                                                                qo_len=q_indptr[b_idx + 1] - q_indptr[b_idx]):
                                                            m_new[i] = T.max(m_new[i], S_smem[row, j])
                                                    d_new[i] = d_smem[row] * T.exp2(m_prev[i] - m_new[i])

                                        for i in T.serial(T.ceildiv(tile_x, bdx * num_warps)):
                                            row: T.int32 = i * bdx * num_warps + ty * bdx + tx
                                            with T.block("update"):
                                                for j in T.serial(tile_z):
                                                    # this is to avoid sync inside condition branch
                                                    if row < tile_x:
                                                        row_: T.int32 = (LH_start + row) // group_size
                                                        if _causal_mask(causal,
                                                                row=row_,
                                                                col=L_kv_start + j,
                                                                kv_len=kv_chunk_len[0],
                                                                qo_len=q_indptr[b_idx + 1] - q_indptr[b_idx]):
                                                            S_smem[row, j] = T.exp2(S_smem[row, j] - m_new[i])
                                                        else:
                                                            S_smem[row, j] = T.exp2(-5e4 - m_new[i])

                                        for i in T.serial(T.ceildiv(tile_x, bdx * num_warps)):
                                            row: T.int32 = i * bdx * num_warps + ty * bdx + tx
                                            if row < tile_x:
                                                with T.block("update"):
                                                    for j in T.serial(tile_z):
                                                        d_new[i] += S_smem[row, j]
                                                    m_smem[row] = m_new[i]
                                                    d_smem[row] = d_new[i]
                                                    m_prev_smem[row] = m_prev[i]
                                        T.tvm_storage_sync("shared")

                                        # Update O
                                        with T.block():
                                            for li, lj, lk in T.grid(tile_x, tile_y, tile_z):
                                                with T.block("O_gemm"):
                                                    i, j, k = T.axis.remap("SSR", [li, lj, lk])
                                                    with T.init():
                                                        O_local[i, j] *= T.exp2(m_prev_smem[i] - m_smem[i])
                                                    O_local[i, j] += S_smem[i, k] * T.cast(V_smem[k, j], "float32")

                                    # Store O from smem to gmem
                                    for li, lj in T.grid(tile_x, tile_y):
                                        with T.block("O_store"):
                                            i, j = T.axis.remap("SS", [li, lj])
                                            cur_L: T.int32 = q_indptr[b_idx] + (LH_start + i) // group_size
                                            cur_H_qo: T.int32 = by * group_size + (LH_start + i) % group_size
                                            if cur_L < q_indptr[b_idx + 1]:
                                                output[cur_L, cur_H_qo, j] = O_local[i, j] / d_smem[i]

                                    # Store LSE to gmem
                                    for li in T.grid(tile_x):
                                        with T.block("lse_store"):
                                            i = T.axis.remap("S", [li])
                                            cur_L: T.int32 = q_indptr[b_idx] + (LH_start + i) // group_size
                                            cur_H_qo: T.int32 = by * group_size + (LH_start + i) % group_size
                                            if cur_L < q_indptr[b_idx + 1]:
                                                lse[cur_L, cur_H_qo] = m_smem[i] + T.log2(d_smem[i])

                                    # move to next tile
                                    tile_id[0] += NUM_BLKS
    # fmt: on
    # pylint: enable=line-too-long,too-many-branches
    sch = tir.Schedule(batch_prefill_paged_kv)
    sch = _schedule_prefill_kernel(
        sch, LOAD_VEC, bdx, num_warps, tile_x, tile_y, tile_z, False, False
    )
    return sch.mod["main"].with_attr("tir.is_scheduled", True)


def _attention_decode_cpu(
    num_kv_heads,
    num_qo_heads,
    head_dim,
    qkv_dtype,
    sliding_window: bool,
    rope_scaling: Dict[str, Any],
    page_size: int = 16,
):
    H_qo = num_qo_heads
    H_kv = num_kv_heads
    D = head_dim
    group_size = num_qo_heads // num_kv_heads

    global_symbol = "batch_decode_paged_kv_cpu"
    if sliding_window:
        global_symbol += "_sliding_window"

    # fmt: off
    # pylint: disable=line-too-long
    @T.prim_func(check_well_formed=False)
    def batch_decode_paged_kv(
        Q_handle: T.handle,
        pages_handle: T.handle,
        page_table_indptr_handle: T.handle,
        page_table_values_handle: T.handle,
        var_length_info: T.handle,  # [b] when sliding window = False, or otherwise [3, b]
        k_rope_pos_offset_handle: T.handle,
        q_rope_position_handle: T.handle,
        output_handle: T.handle,
        lse_handle: T.handle,
        rotary_mode: T.int32,
        rope_scale: T.float32,
        rope_theta: T.float32,
        sm_scale: T.float32,
    ):
        T.func_attr({"tir.is_scheduled": True, "global_symbol": global_symbol})
        B = T.int32(is_size_var=True)
        nnz_pages = T.int32(is_size_var=True)
        max_num_pages = T.int32(is_size_var=True)
        page_indptr_elem_offset = T.int32(is_size_var=True)
        page_values_elem_offset = T.int32(is_size_var=True)
        k_rope_pos_offset_elem_offset = T.int32(is_size_var=True)
        q_rope_position_elem_offset = T.int32(is_size_var=True)
        length_info_elem_offset = T.int32(is_size_var=True)

        Q = T.match_buffer(Q_handle, (B, H_qo, D), qkv_dtype)
        pages = T.match_buffer(pages_handle, (max_num_pages, 2, H_kv, page_size, D), qkv_dtype)
        page_table_indptr = T.match_buffer(
            page_table_indptr_handle, (B + 1,), "int32", elem_offset=page_indptr_elem_offset
        )
        page_table_values = T.match_buffer(
            page_table_values_handle, (nnz_pages,), "int32", elem_offset=page_values_elem_offset
        )
        k_rope_pos_offset = T.match_buffer(
            k_rope_pos_offset_handle, (B,), "int32", elem_offset=k_rope_pos_offset_elem_offset
        )
        q_rope_position = T.match_buffer(
            q_rope_position_handle, (B,), "int32", elem_offset=q_rope_position_elem_offset
        )
        output = T.match_buffer(output_handle, (B, H_qo, D), qkv_dtype)
        lse = T.match_buffer(lse_handle, (B, H_qo), "float32")  # pylint: disable=unused-variable
        # The length information of the sequences.
        # - It is in shape `(3, batch_size)` when sliding window is enabled.
        #   For a sequence "i", location
        #   - "(0, i)" is the number of KV slots used in the last page of the seq ("last_page_len"),
        #   - "(1, i)" is the starting offset of the sliding window in the seq,
        #   - "(2, i)" is the attn sink length of the sequence.
        # - It is in shape `(batch_size,)` when sliding window is disabled,
        #   denoting the "last_page_len".
        length_info = _declare_length_info(
            var_length_info, B, sliding_window, length_info_elem_offset
        )

        for b in T.serial(B):
            with T.block("attn"):
                O_local = T.alloc_buffer((D,), "float32")
                Q_local = T.alloc_buffer((D,), "float32")
                K_local = T.alloc_buffer((D,), "float32")
                V_local = T.alloc_buffer((D,), "float32")

                kv_chunk_len = T.alloc_buffer((1,), "int32")

                m_val = T.alloc_buffer((1,), "float32")
                new_m = T.alloc_buffer((1,), "float32")
                d_val = T.alloc_buffer((1,), "float32")
                S_val = T.alloc_buffer((1,), "float32")
                scale_O = T.alloc_buffer((1,), "float32")
                factor = T.alloc_buffer((1,), "float32")

                cur_page_indptr_begin: T.int32 = page_table_indptr[b]
                cur_page_indptr_end: T.int32 = page_table_indptr[b + 1]

                kv_chunk_len[0] = T.if_then_else(
                    cur_page_indptr_begin != cur_page_indptr_end,
                    _get_kv_chunk_len(cur_page_indptr_end - cur_page_indptr_begin, page_size, b, length_info, sliding_window),
                    0,
                )

                for h_qo in T.serial(H_qo):
                    m_val[0] = -5e4
                    d_val[0] = 1.0

                    for d in T.serial(D):
                        O_local[d] = 0.0

                    for d in T.serial(D):
                        Q_local[d] = T.if_then_else(
                            rotary_mode == 1,
                            _rope(Q, q_rope_position[b], head_dim, rope_theta, rope_scale, (b, h_qo, d), qkv_dtype, rope_scaling),
                            Q[b, h_qo, d],
                        )

                    for row_idx in T.serial(kv_chunk_len[0]):
                        seq_offset: T.int32(is_size_var=True) = _get_seq_offset(row_idx, b, length_info, sliding_window)
                        page_no: T.int32(is_size_var=True) = page_table_values[cur_page_indptr_begin + (seq_offset // page_size)]
                        page_offset: T.int32(is_size_var=True) = seq_offset % page_size

                        for d in T.serial(D):
                            K_local[d] = T.if_then_else(
                                rotary_mode == 1,
                                _rope(pages, k_rope_pos_offset[b] + row_idx, head_dim, rope_theta, rope_scale, (page_no, 0, h_qo // group_size, page_offset, d), qkv_dtype, rope_scaling),
                                pages[page_no, 0, h_qo // group_size, page_offset, d],
                            )
                        S_val[0] = 0.0
                        for d in T.serial(D):
                            S_val[0] += Q_local[d] * K_local[d]
                        S_val[0] *= sm_scale * math.log2(math.exp(1))

                        new_m[0] = T.max(m_val[0], S_val[0])
                        d_val[0] = (d_val[0] * T.exp2(m_val[0] - new_m[0])) + T.exp2(
                            S_val[0] - new_m[0]
                        )

                        scale_O[0] = T.exp2(m_val[0] - new_m[0])

                        for d in T.serial(D):
                            O_local[d] = O_local[d] * scale_O[0]

                        m_val[0] = new_m[0]
                        for d in T.serial(D):
                            V_local[d] = pages[page_no, 1, h_qo // group_size, page_offset, d]

                        factor[0] = T.exp2(S_val[0] - m_val[0])
                        for d in T.serial(D):
                            O_local[d] = O_local[d] + V_local[d] * factor[0]
                    for d in T.serial(D):
                        O_local[d] = O_local[d] / d_val[0]
                        output[b, h_qo, d] = O_local[d]
                    lse[b, h_qo] = m_val[0] + T.log2(d_val[0])
    # fmt: on
    # pylint: enable=line-too-long

    return batch_decode_paged_kv


def _attention_decode(
    num_kv_heads,
    num_qo_heads,
    head_dim,
    qkv_dtype,
    sliding_window: bool,
    rope_scaling: Dict[str, Any],
    target: Target,
    page_size: int = 16,
):
    qkv_dtype_bytes = 2
    H_qo = num_qo_heads
    H_kv = num_kv_heads
    D = head_dim

    THREAD_LIMIT = 512
    TILE_SIZE_PER_BDX = 2
    if target.kind.name == "opencl" and (
        ("android" in str(target.host)) or ("adreno" in str(target.attrs))
    ):
        # Keeping lower thread limit for this kernel on adreno target
        # to avoid register spill
        THREAD_LIMIT = 256
        TILE_SIZE_PER_BDX = 1
    max_num_threads_per_block = get_max_num_threads_per_block(target)
    thread_limit = min(max_num_threads_per_block, THREAD_LIMIT)

    GROUP_SIZE = H_qo // H_kv
    VEC_SIZE = min(max(8 // qkv_dtype_bytes, D // 32), 4)
    bdx = D // VEC_SIZE
    bdy = GROUP_SIZE
    while bdx * bdy > thread_limit and bdy > 1:
        bdy //= 2
    gdz = GROUP_SIZE // bdy
    threads_per_CTA = max(thread_limit, bdx * bdy)
    bdz = threads_per_CTA // (bdx * bdy)
    tile_size_per_bdx = TILE_SIZE_PER_BDX if GROUP_SIZE == 1 else 1
    check_thread_limits(target, bdx=bdx, bdy=bdy, bdz=bdz, gdz=1)

    global_symbol = "batch_decode_paged_kv"
    if sliding_window:
        global_symbol += "_sliding_window"

    # pylint: disable=line-too-long,too-many-branches
    # fmt: off
    @T.prim_func
    def batch_decode_paged_kv(
        Q_handle: T.handle,
        pages_handle: T.handle,
        page_table_indptr_handle: T.handle,
        page_table_values_handle: T.handle,
        var_length_info: T.handle, # [b] when sliding window = False, or otherwise [3, b]
        k_rope_pos_offset_handle: T.handle,
        q_rope_position_handle: T.handle,
        output_handle: T.handle,
        lse_handle: T.handle,
        rotary_mode: T.int32,
        rope_scale: T.float32,
        rope_theta: T.float32,
        sm_scale: T.float32,
    ):
        T.func_attr({"tir.is_scheduled": True, "global_symbol": global_symbol})
        B = T.int32(is_size_var=True)
        nnz_pages = T.int32(is_size_var=True)
        max_num_pages = T.int32(is_size_var=True)
        pages_elem_offset = T.int64(is_size_var=True)
        page_indptr_elem_offset = T.int32(is_size_var=True)
        page_values_elem_offset = T.int32(is_size_var=True)
        k_rope_pos_offset_elem_offset = T.int32(is_size_var=True)
        q_rope_position_elem_offset = T.int32(is_size_var=True)
        length_info_elem_offset = T.int32(is_size_var=True)

        Q = T.match_buffer(Q_handle, (B, H_qo, D), qkv_dtype)
        pages = T.match_buffer(
            pages_handle, (max_num_pages, 2, H_kv, page_size, D), qkv_dtype, elem_offset=pages_elem_offset
        )
        page_table_indptr = T.match_buffer(page_table_indptr_handle, (B + 1,), "int32", elem_offset=page_indptr_elem_offset)
        page_table_values = T.match_buffer(page_table_values_handle, (nnz_pages,), "int32", elem_offset=page_values_elem_offset)
        k_rope_pos_offset = T.match_buffer(k_rope_pos_offset_handle, (B,), "int32", elem_offset=k_rope_pos_offset_elem_offset)
        q_rope_position = T.match_buffer(q_rope_position_handle, (B,), "int32", elem_offset=q_rope_position_elem_offset)
        output = T.match_buffer(output_handle, (B, H_qo, D), qkv_dtype)
        lse = T.match_buffer(lse_handle, (B, H_qo), "float32")  # pylint: disable=unused-variable
        # The length information of the sequences.
        # - It is in shape `(3, batch_size)` when sliding window is enabled.
        #   For a sequence "i", location
        #   - "(0, i)" is the number of KV slots used in the last page of the seq ("last_page_len"),
        #   - "(1, i)" is the starting offset of the sliding window in the seq,
        #   - "(2, i)" is the attn sink length of the sequence.
        # - It is in shape `(batch_size,)` when sliding window is disabled,
        #   denoting the "last_page_len".
        length_info = _declare_length_info(var_length_info, B, sliding_window, length_info_elem_offset)

        for bx in T.thread_binding(B, thread="blockIdx.x"):
            for fused_by_bz in T.thread_binding(H_kv * gdz, thread="blockIdx.y"):
                for ty in T.thread_binding(bdy, thread="threadIdx.y"):
                    for tx in T.thread_binding(bdx, thread="threadIdx.x"):
                        for tz in T.thread_binding(bdz, thread="threadIdx.z"):
                            with T.block("attn"):
                                Q_local = T.alloc_buffer((VEC_SIZE,), qkv_dtype, scope="local")
                                kv_chunk_len = T.alloc_buffer((1,), "int32", scope="local")
                                K_smem = T.alloc_buffer((bdz * bdy * tile_size_per_bdx, D), qkv_dtype, scope="shared")
                                V_smem = T.alloc_buffer((bdz * bdy * tile_size_per_bdx, D), qkv_dtype, scope="shared")
                                O_allreduce = T.alloc_buffer((bdz, bdy, D), "float32", scope="shared")
                                md_allreduce = T.alloc_buffer((bdz, bdy, 2), "float32", scope="shared")
                                S_reduce_local = T.alloc_buffer((1,), "float32", scope="local")
                                t0 = T.alloc_buffer((1,), "float32", scope="local")

                                S_local = T.alloc_buffer((bdy * tile_size_per_bdx), "float32", scope="local")
                                QK_local = T.alloc_buffer((VEC_SIZE,), "float32", scope="local")
                                V_local = T.alloc_buffer((VEC_SIZE,), qkv_dtype, scope="local")
                                m_prev = T.alloc_buffer((1,), "float32", scope="local")
                                d_prev = T.alloc_buffer((1,), "float32", scope="local")
                                other_m = T.alloc_buffer((1,), "float32", scope="local")
                                other_d = T.alloc_buffer((1,), "float32", scope="local")
                                exp_mprev = T.alloc_buffer((1,), "float32", scope="local")
                                exp_otherm = T.alloc_buffer((1,), "float32", scope="local")
                                other_o = T.alloc_buffer((VEC_SIZE,), "float32", scope="local")
                                st_m = T.alloc_buffer((1,), "float32", scope="local")
                                st_d = T.alloc_buffer((1,), "float32", scope="local")
                                O_local = T.alloc_buffer((VEC_SIZE,), "float32", scope="local")

                                by: T.int32 = fused_by_bz % H_kv
                                bz: T.int32 = fused_by_bz // H_kv
                                batch_idx: T.int32 = bx
                                cur_page_indptr_begin: T.int32 = page_table_indptr[batch_idx]
                                cur_page_indptr_end: T.int32 = page_table_indptr[batch_idx + 1]
                                kv_chunk_len[0] = T.if_then_else(
                                    cur_page_indptr_begin != cur_page_indptr_end,
                                    _get_kv_chunk_len(cur_page_indptr_end - cur_page_indptr_begin, page_size, batch_idx, length_info, sliding_window),
                                    0
                                )

                                # init states
                                st_m[0] = -5e4
                                st_d[0] = 1.0
                                for vec in T.vectorized(VEC_SIZE):
                                    O_local[vec] = 0.0

                                # load q
                                for vec in T.vectorized(VEC_SIZE):
                                    Q_local[vec] = T.if_then_else(
                                        rotary_mode == 1,
                                        _rope(Q, q_rope_position[batch_idx], head_dim, rope_theta, rope_scale, (bx, by * GROUP_SIZE + bz * bdy + ty, tx * VEC_SIZE + vec), qkv_dtype, rope_scaling),
                                        Q[bx, by * GROUP_SIZE + bz * bdy + ty, tx * VEC_SIZE + vec]
                                    )

                                for iterator in T.serial(T.ceildiv(kv_chunk_len[0], tile_size_per_bdx * bdy * bdz)):
                                    tile_start_s: T.int32(is_size_var=True) = (tz * bdy + ty) * tile_size_per_bdx  # type: ignore
                                    tile_start_g: T.int32(is_size_var=True) = ((iterator * bdz + tz) * bdy + ty) * tile_size_per_bdx  # type: ignore
                                    # load KV from global memory to shared memory
                                    for j in T.serial(tile_size_per_bdx):
                                        with T.block("KV_load"):
                                            T.reads()
                                            T.writes()
                                            row_g: T.int32(is_size_var=True) = tile_start_g + j  # type: ignore
                                            if row_g < kv_chunk_len[0]:
                                                seq_offset: T.int32(is_size_var=True) = _get_seq_offset(row_g, batch_idx, length_info, sliding_window)  # type: ignore
                                                page_no: T.int32(is_size_var=True) = page_table_values[cur_page_indptr_begin + T.floordiv(seq_offset, page_size)]  # type: ignore
                                                page_offset: T.int32(is_size_var=True) = T.floormod(seq_offset, page_size)  # type: ignore
                                                for vec in T.vectorized(VEC_SIZE):
                                                    K_smem[tile_start_s + j, tx * VEC_SIZE + vec] = T.if_then_else(
                                                        rotary_mode == 1,
                                                        _rope(pages, k_rope_pos_offset[batch_idx] + row_g, head_dim, rope_theta, rope_scale, (page_no, 0, by, page_offset, tx * VEC_SIZE + vec), qkv_dtype, rope_scaling),
                                                        pages[page_no, 0, by, page_offset, tx * VEC_SIZE + vec]
                                                    )
                                                    V_smem[tile_start_s + j, tx * VEC_SIZE + vec] = pages[page_no, 1, by, page_offset, tx * VEC_SIZE + vec]
                                            else:
                                                for vec in T.vectorized(VEC_SIZE):
                                                    K_smem[tile_start_s + j, tx * VEC_SIZE + vec] = 0.0
                                                    V_smem[tile_start_s + j, tx * VEC_SIZE + vec] = 0.0
                                    T.tvm_storage_sync("shared")
                                    # compute QK
                                    m_prev[0] = st_m[0]
                                    for j in T.serial(bdy * tile_size_per_bdx):
                                        # compute S = Q * K * sm_scale
                                        for vec in T.vectorized(VEC_SIZE):
                                            QK_local[vec] = T.cast(Q_local[vec], "float32") * T.cast(K_smem[tz * bdy * tile_size_per_bdx + j, tx * VEC_SIZE + vec], "float32") * sm_scale * math.log2(math.exp(1))
                                        S_reduce_local[0] = 0
                                        for vec in T.unroll(VEC_SIZE):
                                            S_reduce_local[0] += QK_local[vec]

                                        with T.block("block_cross_thread"):
                                            T.reads(S_reduce_local[0])
                                            T.writes(t0[0])
                                            T.attr(
                                                T.comm_reducer(lambda x0, y0: x0 + y0, [T.float32(0)]),
                                                "reduce_scope",
                                                T.reinterpret("handle", T.uint64(0)),
                                            )
                                            T.tvm_thread_allreduce(T.uint32(1), S_reduce_local[0], True, t0[0], tx, dtype="handle")

                                        S_local[j] = -5e4
                                        if (iterator * bdz + tz) * bdy * tile_size_per_bdx + j < kv_chunk_len[0]:
                                            S_local[j] = t0[0]
                                        # update st_m
                                        st_m[0] = T.max(st_m[0], S_local[j])

                                    # update st_d, st_O
                                    o_scale: T.float32 = T.exp2(m_prev[0] - st_m[0])
                                    st_d[0] *= o_scale
                                    for j in T.serial(bdy * tile_size_per_bdx):
                                        S_local[j] = T.exp2(S_local[j] - st_m[0])
                                        st_d[0] += S_local[j]
                                    for j in T.vectorized(VEC_SIZE):
                                        O_local[j] *= o_scale

                                    # load V from shared memory to local memory
                                    # compute O
                                    for j in T.serial(bdy * tile_size_per_bdx):
                                        for vec in T.vectorized(VEC_SIZE):
                                            V_local[vec] = V_smem[tz * bdy * tile_size_per_bdx + j, tx * VEC_SIZE + vec]
                                        for vec in T.vectorized(VEC_SIZE):
                                            O_local[vec] += T.cast(V_local[vec], "float32") * S_local[j]

                                if bdz > 1:
                                    # allreduce over bdz
                                    for vec in T.vectorized(VEC_SIZE):
                                        O_allreduce[tz, ty, tx * VEC_SIZE + vec] = O_local[vec]
                                    md_allreduce[tz, ty, 0] = st_m[0]
                                    md_allreduce[tz, ty, 1] = st_d[0]
                                    T.tvm_storage_sync("shared")

                                    st_m[0] = -5e4
                                    st_d[0] = 1.0
                                    for vec in T.vectorized(VEC_SIZE):
                                        O_local[vec] = 0.0

                                    for j in T.serial(bdz):
                                        m_prev[0] = st_m[0]
                                        d_prev[0] = st_d[0]
                                        other_m[0] = md_allreduce[j, ty, 0]
                                        other_d[0] = md_allreduce[j, ty, 1]
                                        for vec in T.vectorized(VEC_SIZE):
                                            other_o[vec] = O_allreduce[j, ty, tx * VEC_SIZE + vec]
                                        st_m[0] = T.max(st_m[0], other_m[0])
                                        st_d[0] = d_prev[0] * T.exp2(m_prev[0] - st_m[0]) + other_d[0] * T.exp2(other_m[0] - st_m[0])
                                        exp_mprev[0] = T.exp2(m_prev[0] - st_m[0])
                                        exp_otherm[0] = T.exp2(other_m[0] - st_m[0])
                                        for vec in T.vectorized(VEC_SIZE):
                                            O_local[vec] = O_local[vec] * exp_mprev[0] + other_o[vec] * exp_otherm[0]

                                # normalize O
                                for vec in T.vectorized(VEC_SIZE):
                                    O_local[vec] /= st_d[0]

                                # store O to global memory
                                for vec in T.vectorized(VEC_SIZE):
                                    output[batch_idx, by * GROUP_SIZE + bz * bdy + ty, tx * VEC_SIZE + vec] = O_local[vec]

                                # store lse to global memory
                                lse[batch_idx, by * GROUP_SIZE + bz * bdy + ty] = st_m[0] + T.log2(st_d[0])
    # fmt: on
    # pylint: enable=line-too-long,too-many-branches
    return batch_decode_paged_kv


def _merge_state_inplace_cpu(v_dtype):
    @T.prim_func
    def merge_state_inplace_cpu(
        v: T.handle,
        s: T.handle,
        v_other: T.handle,
        s_other: T.handle,
    ):
        T.func_attr({"tir.is_scheduled": True})
        N = T.int32(is_size_var=True)
        H = T.int32(is_size_var=True)
        D = T.int32(is_size_var=True)

        V = T.match_buffer(v, (N, H, D), v_dtype)
        S = T.match_buffer(s, (N, H), "float32")
        V_other = T.match_buffer(v_other, (N, H, D), v_dtype)
        S_other = T.match_buffer(s_other, (N, H), "float32")

        for n in T.serial(N):
            for h in T.serial(H):
                with T.block("merge"):
                    s_val = _var_cpu("float32")
                    s_other_val = _var_cpu("float32")
                    s_max = _var_cpu("float32")
                    scale = _var_cpu("float32")
                    other_scale = _var_cpu("float32")

                    s_val[0] = S[n, h]
                    s_other_val[0] = S_other[n, h]
                    s_max[0] = T.max(s_val[0], s_other_val[0])
                    s_val[0] = T.exp2(s_val[0] - s_max[0])
                    s_other_val[0] = T.exp2(s_other_val[0] - s_max[0])
                    scale[0] = s_val[0] / (s_val[0] + s_other_val[0])
                    other_scale[0] = s_other_val[0] / (s_val[0] + s_other_val[0])
                    for d in T.serial(D):
                        V[n, h, d] = V[n, h, d] * scale[0] + V_other[n, h, d] * other_scale[0]
                    S[n, h] = T.log2(s_val[0] + s_other_val[0]) + s_max[0]

    return merge_state_inplace_cpu


def _merge_state_inplace(
    num_heads, head_dim, v_dtype, target: Target, global_symbol: Optional[str] = None
):
    v_dtype_bytes = 2
    VEC_SIZE = min(max(8 // v_dtype_bytes, head_dim // 32), 4)
    bdx = head_dim // VEC_SIZE
    bdy = num_heads
    max_num_threads_per_block = get_max_num_threads_per_block(target)
    while bdx * bdy > max_num_threads_per_block and bdy > 1:
        bdy //= 2
    gdy = num_heads // bdy
    check_thread_limits(target, bdx=bdx, bdy=bdy, bdz=1, gdz=1)

    @T.prim_func
    def merge_state_inplace(
        v: T.handle,
        s: T.handle,
        v_other: T.handle,
        s_other: T.handle,
    ):
        T.func_attr({"tir.is_scheduled": True})
        N = T.int32(is_size_var=True)
        H = T.int32(is_size_var=True)
        D = T.int32(is_size_var=True)

        V = T.match_buffer(v, (N, H, D), v_dtype)
        S = T.match_buffer(s, (N, H), "float32")
        V_other = T.match_buffer(v_other, (N, H, D), v_dtype)
        S_other = T.match_buffer(s_other, (N, H), "float32")

        for bx in T.thread_binding(N, thread="blockIdx.x"):
            for by in T.thread_binding(gdy, thread="blockIdx.y"):
                for ty in T.thread_binding(bdy, thread="threadIdx.y"):
                    for tx in T.thread_binding(bdx, thread="threadIdx.x"):
                        with T.block("merge"):
                            s_val = _var("float32")
                            s_other_val = _var("float32")
                            s_max = _var("float32")
                            scale = _var("float32")
                            other_scale = _var("float32")

                            v_vec = T.alloc_buffer((VEC_SIZE,), v_dtype, scope="local")
                            v_other_vec = T.alloc_buffer((VEC_SIZE,), v_dtype, scope="local")

                            s_val[0] = S[bx, ty + by * bdy]
                            s_other_val[0] = S_other[bx, ty + by * bdy]
                            s_max[0] = T.max(s_val[0], s_other_val[0])
                            s_val[0] = T.exp2(s_val[0] - s_max[0])
                            s_other_val[0] = T.exp2(s_other_val[0] - s_max[0])
                            scale[0] = s_val[0] / (s_val[0] + s_other_val[0])
                            other_scale[0] = s_other_val[0] / (s_val[0] + s_other_val[0])

                            # load v
                            for vec in T.vectorized(VEC_SIZE):
                                v_vec[vec] = V[bx, ty + by * bdy, tx * VEC_SIZE + vec]
                            # load v_other
                            for vec in T.vectorized(VEC_SIZE):
                                v_other_vec[vec] = V_other[bx, ty + by * bdy, tx * VEC_SIZE + vec]

                            # merge
                            for vec in T.serial(VEC_SIZE):
                                v_vec[vec] = (
                                    v_vec[vec] * scale[0] + v_other_vec[vec] * other_scale[0]
                                )

                            # store v
                            for vec in T.vectorized(VEC_SIZE):
                                V[bx, ty + by * bdy, tx * VEC_SIZE + vec] = v_vec[vec]

                            # store s
                            S[bx, ty + by * bdy] = T.log2(s_val[0] + s_other_val[0]) + s_max[0]

    func = merge_state_inplace
    if global_symbol:
        func = func.with_attr("global_symbol", global_symbol)
    return func


def _attention_sequence_prefill(
    h_kv, h_q, d, dtype, target: Target, causal=0, sm_scale=1.0
):  # pylint: disable=line-too-long
    (
        _,
        LOAD_VEC,
        group_size,
        bdx,
        num_warps,
        tile_x,
        tile_y,
        tile_z,
    ) = _get_prefill_kernel_config(h_kv, h_q, d, dtype, target)

    # fmt: off
    @T.prim_func
    def batch_sequence_prefill_kv(  # pylint: disable=too-many-branches
        var_q: T.handle, # [total_len, h_q, d]
        var_k: T.handle, # [total_len, h_kv, d]
        var_v: T.handle, # [total_len, h_kv, d]
        var_output: T.handle, # [total_len, h_q, d]
        var_lse: T.handle # [total_len, h_q]
    ):
        batch_size = T.int32(is_size_var=True)
        qo_len = T.int32(is_size_var=True)
        kv_len = T.int32(is_size_var=True)
        q = T.match_buffer(var_q, (batch_size, qo_len, h_q, d), dtype)
        k = T.match_buffer(var_k, (batch_size, kv_len, h_kv, d), dtype)
        v = T.match_buffer(var_v, (batch_size, kv_len, h_kv, d), dtype)
        output = T.match_buffer(var_output, (batch_size, qo_len, h_q, d), dtype)
        lse = T.match_buffer(var_lse, (batch_size, qo_len, h_q), dtype)  # pylint: disable=unused-variable

        batch_tiles: T.int32 = T.ceildiv(qo_len * group_size, tile_x)

        # kernel code
        for lbx in T.thread_binding(T.cast(batch_size, "int32") * batch_tiles, thread="blockIdx.x"):
            for lby in T.thread_binding(h_kv, thread="blockIdx.y"):
                for lty in T.thread_binding(num_warps, thread="threadIdx.y"):
                    for ltx in T.thread_binding(bdx, thread="threadIdx.x"):
                        with T.block("attn"):
                            vbx, by, ty, tx = T.axis.remap("SSSS", [lbx, lby, lty, ltx])
                            T.reads()
                            T.writes()

                            Q_smem = T.alloc_buffer((tile_x, d), dtype, scope="shared")
                            K_smem = T.alloc_buffer((tile_z, d), dtype, scope="shared")
                            V_smem = T.alloc_buffer((tile_z, d), dtype, scope="shared")
                            S_smem = T.alloc_buffer((tile_x, tile_z), "float32", scope="shared")

                            S_local = T.alloc_buffer((tile_x, tile_z), "float32", scope="local")
                            O_local = T.alloc_buffer((tile_x, d), "float32", scope="local")

                            m_smem = T.alloc_buffer((tile_x,), "float32", scope="shared")
                            m_prev_smem = T.alloc_buffer((tile_x,), "float32", scope="shared")
                            d_smem = T.alloc_buffer((tile_x,), "float32", scope="shared")

                            m_new = T.alloc_buffer(
                                (math.ceil(tile_x / (bdx * num_warps)),), "float32", scope="local"
                            )
                            m_prev = T.alloc_buffer(
                                (math.ceil(tile_x / (bdx * num_warps)),), "float32", scope="local"
                            )
                            d_new = T.alloc_buffer(
                                (math.ceil(tile_x / (bdx * num_warps)),), "float32", scope="local"
                            )

                            b_idx: T.int32 = vbx // batch_tiles
                            tile_id: T.int32 = vbx % batch_tiles
                            LH_start: T.int32 = tile_id * tile_x
                            T.tvm_storage_sync("shared")

                            # init states
                            for i in T.serial(T.ceildiv(tile_x, bdx * num_warps)):
                                row: T.int32 = i * bdx * num_warps + ty * bdx + tx
                                if row < tile_x:
                                    m_smem[row] = -5e4
                                    d_smem[row] = 1.0

                            for li, lj in T.grid(tile_x, tile_y):
                                with T.block("O_init"):
                                    i, j = T.axis.remap("SS", [li, lj])
                                    O_local[i, j] = 0.0
                            T.tvm_storage_sync("shared")

                            # Load Q from gmem to smem
                            for li, lj in T.grid(tile_x, tile_y):
                                with T.block("Q_load"):
                                    i, j = T.axis.remap("SS", [li, lj])
                                    T.reads()
                                    T.writes()
                                    cur_L = (LH_start + i) // group_size
                                    cur_H_qo = by * group_size + (LH_start + i) % group_size
                                    if cur_L < qo_len:
                                        Q_smem[i, j] = q[b_idx, cur_L, cur_H_qo, j]
                                    else:
                                        Q_smem[i, j] = 0.0
                            T.tvm_storage_sync("shared")

                            for iterator in T.serial(T.ceildiv(kv_len, tile_z)):
                                L_kv_start: T.int32 = iterator * tile_z
                                L_kv_base: T.int32 = 0
                                for lz, ly in T.grid(tile_z, tile_y):
                                    with T.block("K_load"):
                                        i, j = T.axis.remap("SS", [lz, ly])
                                        T.reads()
                                        T.writes()
                                        cur_L = L_kv_start + i
                                        if cur_L < kv_len:
                                            K_smem[i, j] = k[
                                                b_idx, L_kv_base + cur_L, by, j
                                            ]
                                        else:
                                            K_smem[i, j] = 0.0
                                T.tvm_storage_sync("shared")
                                for lz, ly in T.grid(tile_z, tile_y):
                                    with T.block("V_load"):
                                        i, j = T.axis.remap("SS", [lz, ly])
                                        T.reads()
                                        T.writes()
                                        cur_L = L_kv_start + i
                                        if cur_L < kv_len:
                                            V_smem[i, j] = v[
                                                b_idx, L_kv_base + cur_L, by, j
                                            ]
                                        else:
                                            V_smem[i, j] = 0.0
                                T.tvm_storage_sync("shared")

                                # Compute S
                                with T.block():
                                    for li, lj, lk in T.grid(tile_x, tile_z, tile_y):
                                        with T.block("S_gemm"):
                                            i, j, k = T.axis.remap("SSR", [li, lj, lk])
                                            with T.init():
                                                S_local[i, j] = 0.0
                                            S_local[i, j] += (
                                                T.cast(Q_smem[i, k], "float32")
                                                * T.cast(K_smem[j, k], "float32")
                                                * sm_scale
                                                * math.log2(math.exp(1))
                                            )
                                T.tvm_storage_sync("shared")
                                for li, lj in T.grid(tile_x, tile_z):
                                    with T.block("S_store"):
                                        i, j = T.axis.remap("SS", [li, lj])
                                        S_smem[i, j] = S_local[i, j]
                                T.tvm_storage_sync("shared")

                                # Update S, m, d
                                for i in T.serial(T.ceildiv(tile_x, bdx * num_warps)):
                                    row: T.int32 = i * bdx * num_warps + ty * bdx + tx
                                    if row < tile_x:
                                        with T.block("update1"):
                                            m_prev[i] = m_smem[row]
                                            m_new[i] = m_smem[row]
                                            # mask out of kv_chunk_len S
                                            row_: T.int32 = (LH_start + row) // group_size
                                            for j in T.serial(tile_z):
                                                if _causal_mask(
                                                    causal,
                                                    row=row_,
                                                    col=L_kv_start + j,
                                                    kv_len=kv_len,
                                                    qo_len=qo_len,
                                                ):
                                                    m_new[i] = T.max(
                                                        m_new[i], S_smem[row, j]
                                                    )
                                            d_new[i] = d_smem[row] * T.exp2(
                                                m_prev[i] - m_new[i]
                                            )

                                for i in T.serial(T.ceildiv(tile_x, bdx * num_warps)):
                                    row: T.int32 = i * bdx * num_warps + ty * bdx + tx
                                    with T.block("update"):
                                        for j in T.serial(tile_z):
                                            # this is to avoid sync inside condition branch
                                            if row < tile_x:
                                                row_: T.int32 = (
                                                    LH_start + row
                                                ) // group_size
                                                if _causal_mask(
                                                    causal,
                                                    row=row_,
                                                    col=L_kv_start + j,
                                                    kv_len=kv_len,
                                                    qo_len=qo_len,
                                                ):
                                                    S_smem[row, j] = T.exp2(
                                                        S_smem[row, j] - m_new[i]
                                                    )
                                                else:
                                                    S_smem[row, j] = T.exp2(-5e4 - m_new[i])

                                for i in T.serial(T.ceildiv(tile_x, bdx * num_warps)):
                                    row: T.int32 = i * bdx * num_warps + ty * bdx + tx
                                    if row < tile_x:
                                        with T.block("update"):
                                            for j in T.serial(tile_z):
                                                d_new[i] += S_smem[row, j]
                                            m_smem[row] = m_new[i]
                                            d_smem[row] = d_new[i]
                                            m_prev_smem[row] = m_prev[i]
                                T.tvm_storage_sync("shared")

                                # Update O
                                with T.block():
                                    for li, lj, lk in T.grid(tile_x, tile_y, tile_z):
                                        with T.block("O_gemm"):
                                            i, j, k = T.axis.remap("SSR", [li, lj, lk])
                                            with T.init():
                                                O_local[i, j] *= T.exp2(
                                                    m_prev_smem[i] - m_smem[i]
                                                )
                                            O_local[i, j] += S_smem[i, k] * T.cast(
                                                V_smem[k, j], "float32"
                                            )

                            # Store O from smem to gmem
                            for li, lj in T.grid(tile_x, tile_y):
                                with T.block("O_store"):
                                    i, j = T.axis.remap("SS", [li, lj])
                                    cur_L: T.int32 = 0 + (LH_start + i) // group_size
                                    cur_H_qo: T.int32 = (
                                        by * group_size + (LH_start + i) % group_size
                                    )
                                    if cur_L < qo_len:
                                        output[b_idx, cur_L, cur_H_qo, j] = (
                                            O_local[i, j] / d_smem[i]
                                        )

                            # Store LSE to gmem
                            for li in T.grid(tile_x):
                                with T.block("lse_store"):
                                    i = T.axis.remap("S", [li])
                                    cur_L: T.int32 = 0 + (LH_start + i) // group_size
                                    cur_H_qo: T.int32 = (
                                        by * group_size + (LH_start + i) % group_size
                                    )
                                    if cur_L < qo_len:
                                        lse[b_idx, cur_L, cur_H_qo] = m_smem[i] + T.log2(
                                            d_smem[i]
                                        )

    # fmt: on
    # pylint: enable=line-too-long,too-many-branches
    sch = tir.Schedule(batch_sequence_prefill_kv)
    sch = _schedule_prefill_kernel(
        sch, LOAD_VEC, bdx, num_warps, tile_x, tile_y, tile_z, False, False
    )
    return sch.mod["main"].with_attr("tir.is_scheduled", True)


def _attention_prefill_ragged_cpu(h_kv, h_q, d_qk, d_v, dtype, rope_scaling: Dict[str, Any]):
    group_size = h_q // h_kv

    # fmt: off
    # pylint: disable=line-too-long
    @T.prim_func
    def batch_prefill_ragged_kv(  # pylint: disable=too-many-branches
        var_q: T.handle,  # [total_len, h_q, d_qk]
        var_q_indptr: T.handle,  # [batch_size + 1]
        var_k: T.handle,  # [total_len, h_kv, d_qk]
        var_v: T.handle,  # [total_len, h_kv, d_v]
        var_kv_indptr: T.handle,  # [batch_size + 1]
        var_q_rope_position: T.handle,  # [total_q_len]
        var_k_rope_pos_offset: T.handle,  # [b]
        var_output: T.handle,  # [total_len, h_q, d_v]
        var_lse: T.handle,  # [total_len, h_q]
        causal: T.int32,
        rotary_mode: T.int32,
        rope_scale: T.float32,
        rope_theta: T.float32,
        sm_scale: T.float32,
    ):
        batch_size = T.int32(is_size_var=True)
        qo_len = T.int32(is_size_var=True)
        kv_len = T.int32(is_size_var=True)
        q_indptr_elem_offset = T.int32(is_size_var=True)
        kv_indptr_elem_offset = T.int32(is_size_var=True)
        q_rope_position_elem_offset = T.int32(is_size_var=True)
        k_rope_pos_offset_elem_offset = T.int32(is_size_var=True)

        q = T.match_buffer(var_q, (qo_len, h_q, d_qk), dtype)
        q_indptr = T.match_buffer(
            var_q_indptr, (batch_size + 1,), "int32", elem_offset=q_indptr_elem_offset
        )
        k = T.match_buffer(var_k, (kv_len, h_kv, d_qk), dtype)
        v = T.match_buffer(var_v, (kv_len, h_kv, d_v), dtype)
        kv_indptr = T.match_buffer(
            var_kv_indptr, (batch_size + 1,), "int32", elem_offset=kv_indptr_elem_offset
        )
        q_rope_position = T.match_buffer(
            var_q_rope_position, (qo_len,), "int32", elem_offset=q_rope_position_elem_offset
        )
        k_rope_pos_offset = T.match_buffer(
            var_k_rope_pos_offset, (batch_size,), "int32", elem_offset=k_rope_pos_offset_elem_offset
        )
        output = T.match_buffer(var_output, (qo_len, h_q, d_v), dtype)
        lse = T.match_buffer(var_lse, (qo_len, h_q), "float32")  # pylint: disable=unused-variable

        for b in T.serial(batch_size):
            with T.block("attn"):
                softmax_sum = T.alloc_buffer([h_q], "float32")
                m_prev = T.alloc_buffer([h_q], "float32")
                m_new = T.alloc_buffer([h_q], "float32")
                d_prev = T.alloc_buffer([h_q], "float32")
                d_new = T.alloc_buffer([h_q], "float32")
                p_sum = T.alloc_buffer([d_v], "float32")
                max_score = T.alloc_buffer([h_q], "float32")
                attention_scores = T.alloc_buffer([kv_len, h_q], "float32")
                exp_scores = T.alloc_buffer([kv_len, h_q], "float32")
                attention_score = T.alloc_buffer([1], "float32")
                query_val = T.alloc_buffer([1], "float32")
                key_val = T.alloc_buffer([1], "float32")
                result = T.alloc_buffer([1], "float32")

                for q_idx in T.serial(q_indptr[b + 1] - q_indptr[b]):
                    for i in T.serial(h_q):
                        max_score[i] = -5e4
                        m_prev[i] = -5e4
                        d_prev[i] = 1.0

                    for k_idx in T.serial(kv_indptr[b + 1] - kv_indptr[b]):
                        for h in T.serial(h_q):
                            h_kv_idx = h // group_size

                            if _causal_mask(
                                causal,
                                row=q_idx,
                                col=k_idx,
                                kv_len=kv_indptr[b + 1] - kv_indptr[b],
                                qo_len=q_indptr[b + 1] - q_indptr[b],
                            ):
                                result[0] = 0.0
                                for d_idx in T.serial(d_qk):
                                    query_val[0] = T.if_then_else(
                                        rotary_mode == 1,
                                        _rope(q, q_rope_position[q_indptr[b] + q_idx], d_qk, rope_theta, rope_scale, (q_indptr[b] + q_idx, h, d_idx), dtype, rope_scaling),
                                        q[q_indptr[b] + q_idx, h, d_idx],
                                    )

                                    key_val[0] = T.if_then_else(
                                        rotary_mode == 1,
                                        _rope(k, k_rope_pos_offset[b] + k_idx, d_qk, rope_theta, rope_scale, (kv_indptr[b] + k_idx, h_kv_idx, d_idx), dtype, rope_scaling),
                                        k[kv_indptr[b] + k_idx, h_kv_idx, d_idx],
                                    )

                                    result[0] += query_val[0] * key_val[0]
                                attention_score[0] = result[0] * math.log2(math.exp(1)) * sm_scale
                            else:
                                attention_score[0] = -5e4 * math.log2(math.exp(1)) * sm_scale
                            attention_scores[k_idx, h] = attention_score[0]
                            max_score[h] = T.max(max_score[h], attention_score[0])
                            m_new[h] = T.max(m_prev[h], max_score[h])

                    for h in T.serial(h_q):
                        d_new[h] = d_prev[h] * T.exp2(m_prev[h] - m_new[h])

                    for h in T.serial(h_q):
                        softmax_sum[h] = 0.0
                        for k_idx in T.serial(kv_indptr[b + 1] - kv_indptr[b]):
                            exp_scores[k_idx, h] = T.exp2(attention_scores[k_idx, h] - m_new[h])
                            softmax_sum[h] += exp_scores[k_idx, h]
                        d_new[h] += softmax_sum[h]
                    d_prev = d_new
                    m_prev = m_new

                    for h in T.serial(h_q):
                        h_kv_idx = h // group_size
                        for i in T.serial(d_v):
                            p_sum[i] = 0.0
                        for v_idx in T.serial(kv_indptr[b + 1] - kv_indptr[b]):
                            weight = exp_scores[v_idx, h] / d_new[h]
                            for i in T.serial(d_v):
                                p_sum[i] += v[kv_indptr[b] + v_idx, h_kv_idx, i] * weight
                        for i in T.serial(d_v):
                            output[q_indptr[b] + q_idx, h, i] = p_sum[i]
                        lse[q_indptr[b] + q_idx, h] = m_prev[h] + T.log2(d_prev[h])
    # fmt: on
    # pylint: enable=line-too-long
    return batch_prefill_ragged_kv


def _attention_prefill_ragged(
    h_kv, h_q, d_qk, d_v, dtype, rope_scaling: Dict[str, Any], target: Target
):
    # pylint: disable=line-too-long
    (
        NUM_BLKS,
        LOAD_VEC,
        group_size,
        bdx,
        num_warps,
        tile_x,
        tile_y,
        tile_z,
    ) = _get_prefill_kernel_config(h_kv, h_q, d_qk, dtype, target)

    # fmt: off
    @T.prim_func
    def batch_prefill_ragged_kv(  # pylint: disable=too-many-branches
        var_q: T.handle, # [total_len, h_q, d_qk]
        var_q_indptr: T.handle, # [batch_size + 1]
        var_k: T.handle, # [total_len, h_kv, d_qk]
        var_v: T.handle, # [total_len, h_kv, d_v]
        var_kv_indptr: T.handle, # [batch_size + 1]
        var_q_rope_position: T.handle, # [total_q_len]
        var_k_rope_pos_offset: T.handle, # [b]
        var_output: T.handle, # [total_len, h_q, d_v]
        var_lse: T.handle, # [total_len, h_q]
        causal: T.int32,
        rotary_mode: T.int32,
        rope_scale: T.float32,
        rope_theta: T.float32,
        sm_scale: T.float32
    ):
        batch_size = T.int32(is_size_var=True)
        qo_len = T.int32(is_size_var=True)
        kv_len = T.int32(is_size_var=True)
        q_indptr_elem_offset = T.int32(is_size_var=True)
        kv_indptr_elem_offset = T.int32(is_size_var=True)
        q_rope_position_elem_offset = T.int32(is_size_var=True)
        k_rope_pos_offset_elem_offset = T.int32(is_size_var=True)

        q = T.match_buffer(var_q, (qo_len, h_q, d_qk), dtype)
        q_indptr = T.match_buffer(var_q_indptr, (batch_size + 1,), "int32", elem_offset=q_indptr_elem_offset)
        k = T.match_buffer(var_k, (kv_len, h_kv, d_qk), dtype)
        v = T.match_buffer(var_v, (kv_len, h_kv, d_v), dtype)
        kv_indptr = T.match_buffer(var_kv_indptr, (batch_size + 1,), "int32", elem_offset=kv_indptr_elem_offset)
        q_rope_position = T.match_buffer(var_q_rope_position, (qo_len,), "int32", elem_offset=q_rope_position_elem_offset)
        k_rope_pos_offset = T.match_buffer(var_k_rope_pos_offset, (batch_size,), "int32", elem_offset=k_rope_pos_offset_elem_offset)
        output = T.match_buffer(var_output, (qo_len, h_q, d_v), dtype)
        lse = T.match_buffer(var_lse, (qo_len, h_q), "float32")  # pylint: disable=unused-variable

        # kernel code
        for lbx in T.thread_binding(NUM_BLKS, thread="blockIdx.x"):
            for lby in T.thread_binding(h_kv, thread="blockIdx.y"):
                for lty in T.thread_binding(num_warps, thread="threadIdx.y"):
                    for ltx in T.thread_binding(bdx, thread="threadIdx.x"):
                        with T.block("attn"):
                            bx, by, ty, tx = T.axis.remap("SSSS", [lbx, lby, lty, ltx])
                            T.reads()
                            T.writes()
                            tile_id = _var("int32")
                            batch_idx = _var("int32")
                            batch_tiles = _var("int32")
                            batch_rows = _var("int32")
                            iterator = _var("int32")
                            kv_chunk_len = _var("int32")

                            Q_smem = T.alloc_buffer((tile_x, d_qk), dtype, scope="shared")
                            K_smem = T.alloc_buffer((tile_z, d_qk), dtype, scope="shared")
                            V_smem = T.alloc_buffer((tile_z, d_v), dtype, scope="shared")
                            S_smem = T.alloc_buffer((tile_x, tile_z), "float32", scope="shared")

                            S_local = T.alloc_buffer((tile_x, tile_z), "float32", scope="local")
                            O_local = T.alloc_buffer((tile_x, d_v), "float32", scope="local")

                            m_smem = T.alloc_buffer((tile_x, ), "float32", scope="shared")
                            m_prev_smem = T.alloc_buffer((tile_x, ), "float32", scope="shared")
                            d_smem = T.alloc_buffer((tile_x, ), "float32", scope="shared")

                            m_new = T.alloc_buffer((math.ceil(tile_x / (bdx * num_warps)),), "float32", scope="local")
                            m_prev = T.alloc_buffer((math.ceil(tile_x / (bdx * num_warps)),), "float32", scope="local")
                            d_new = T.alloc_buffer((math.ceil(tile_x / (bdx * num_warps)),), "float32", scope="local")

                            ## get tile_no, batch_idx, batch_tiles, batch_rows
                            tile_id[0] = bx
                            batch_idx[0] = 0
                            batch_rows[0] = (q_indptr[1] - q_indptr[0]) * group_size
                            batch_tiles[0] = T.ceildiv(batch_rows[0], tile_x)
                            while T.tvm_thread_invariant(batch_idx[0] < batch_size):
                                # advance to next tile
                                while tile_id[0] >= batch_tiles[0] and batch_idx[0] < batch_size:
                                    tile_id[0] -= batch_tiles[0]
                                    batch_idx[0] += 1
                                    if batch_idx[0] < batch_size:
                                        b_idx: T.int32 = batch_idx[0]
                                        batch_rows[0] = (q_indptr[b_idx + 1] - q_indptr[b_idx]) * group_size
                                        batch_tiles[0] = T.ceildiv(batch_rows[0], tile_x)

                                if T.tvm_thread_invariant(batch_idx[0] < batch_size):
                                    b_idx: T.int32 = batch_idx[0]
                                    q_indptr_val: T.int32 = q_indptr[b_idx]
                                    LH_start: T.int32 = tile_id[0] * tile_x

                                    kv_chunk_len[0] = kv_indptr[b_idx + 1] - kv_indptr[b_idx]
                                    T.tvm_storage_sync("shared")

                                    # init states
                                    for i in T.serial(T.ceildiv(tile_x, bdx * num_warps)):
                                        row: T.int32 = i * bdx * num_warps + ty * bdx + tx
                                        if row < tile_x:
                                            m_smem[row] = -5e4
                                            d_smem[row] = 1.0

                                    for li, lj in T.grid(tile_x, d_v):
                                        with T.block("O_init"):
                                            i, j = T.axis.remap("SS", [li, lj])
                                            O_local[i, j] = 0.0
                                    T.tvm_storage_sync("shared")

                                    # Load Q from gmem to smem
                                    for li, lj in T.grid(tile_x, tile_y):
                                        with T.block("Q_load"):
                                            i, j = T.axis.remap("SS", [li, lj])
                                            T.reads()
                                            T.writes()
                                            cur_L = q_indptr_val + (LH_start + i) // group_size
                                            cur_H_qo = by * group_size + (LH_start + i) % group_size
                                            if cur_L < q_indptr[b_idx + 1]:
                                                Q_smem[i, j] = T.if_then_else(
                                                    rotary_mode == 1,
                                                    _rope(q, q_rope_position[cur_L], d_qk, rope_theta, rope_scale, (cur_L, cur_H_qo, j), dtype, rope_scaling),
                                                    q[cur_L, cur_H_qo, j]
                                                )
                                            else:
                                                Q_smem[i, j] = 0.0
                                    T.tvm_storage_sync("shared")

                                    for iterator in T.serial(T.ceildiv(kv_chunk_len[0], tile_z)):
                                        L_kv_start: T.int32 = iterator * tile_z
                                        L_kv_base: T.int32 = kv_indptr[b_idx]
                                        for lz, ly in T.grid(tile_z, tile_y):
                                            with T.block("K_load"):
                                                i, j = T.axis.remap("SS", [lz, ly])
                                                cur_L = L_kv_start + i
                                                if cur_L < kv_chunk_len[0]:
                                                    K_smem[i, j] = T.if_then_else(
                                                        rotary_mode == 1,
                                                        _rope(k, k_rope_pos_offset[b_idx] + cur_L, d_qk, rope_theta, rope_scale, (L_kv_base + cur_L, by, j), dtype, rope_scaling),
                                                        k[L_kv_base + cur_L, by, j]
                                                    )
                                                else:
                                                    K_smem[i, j] = 0.0
                                        T.tvm_storage_sync("shared")
                                        for lz, ly in T.grid(tile_z, d_v):
                                            with T.block("V_load"):
                                                i, j = T.axis.remap("SS", [lz, ly])
                                                T.reads()
                                                T.writes()
                                                cur_L = L_kv_start + i
                                                if cur_L < kv_chunk_len[0]:
                                                    V_smem[i, j] = v[L_kv_base + cur_L, by, j]
                                                else:
                                                    V_smem[i, j] = 0.0
                                        T.tvm_storage_sync("shared")

                                        # Compute S
                                        with T.block():
                                            for li, lj, lk in T.grid(tile_x, tile_z, tile_y):
                                                with T.block("S_gemm"):
                                                    i, j, k = T.axis.remap("SSR", [li, lj, lk])
                                                    with T.init():
                                                        S_local[i, j] = 0.0
                                                    S_local[i, j] += T.cast(Q_smem[i, k], "float32") * T.cast(K_smem[j, k], "float32") * sm_scale * math.log2(math.exp(1))
                                        T.tvm_storage_sync("shared")
                                        for li, lj in T.grid(tile_x, tile_z):
                                            with T.block("S_store"):
                                                i, j = T.axis.remap("SS", [li, lj])
                                                S_smem[i, j] = S_local[i, j]
                                        T.tvm_storage_sync("shared")

                                        # Update S, m, d
                                        for i in T.serial(T.ceildiv(tile_x, bdx * num_warps)):
                                            row: T.int32 = i * bdx * num_warps + ty * bdx + tx
                                            if row < tile_x:
                                                with T.block("update1"):
                                                    m_prev[i] = m_smem[row]
                                                    m_new[i] = m_smem[row]
                                                    # mask out of kv_chunk_len S
                                                    row_: T.int32 = (LH_start + row) // group_size
                                                    for j in T.serial(tile_z):
                                                        if _causal_mask(causal,
                                                                row=row_,
                                                                col=L_kv_start + j,
                                                                kv_len=kv_chunk_len[0],
                                                                qo_len=q_indptr[b_idx + 1] - q_indptr[b_idx]):
                                                            m_new[i] = T.max(m_new[i], S_smem[row, j])
                                                    d_new[i] = d_smem[row] * T.exp2(m_prev[i] - m_new[i])

                                        for i in T.serial(T.ceildiv(tile_x, bdx * num_warps)):
                                            row: T.int32 = i * bdx * num_warps + ty * bdx + tx
                                            with T.block("update"):
                                                for j in T.serial(tile_z):
                                                    # this is to avoid sync inside condition branch
                                                    if row < tile_x:
                                                        row_: T.int32 = (LH_start + row) // group_size
                                                        if _causal_mask(causal,
                                                                row=row_,
                                                                col=L_kv_start + j,
                                                                kv_len=kv_chunk_len[0],
                                                                qo_len=q_indptr[b_idx + 1] - q_indptr[b_idx]):
                                                            S_smem[row, j] = T.exp2(S_smem[row, j] - m_new[i])
                                                        else:
                                                            S_smem[row, j] = T.exp2(-5e4 - m_new[i])

                                        for i in T.serial(T.ceildiv(tile_x, bdx * num_warps)):
                                            row: T.int32 = i * bdx * num_warps + ty * bdx + tx
                                            if row < tile_x:
                                                with T.block("update"):
                                                    for j in T.serial(tile_z):
                                                        d_new[i] += S_smem[row, j]
                                                    m_smem[row] = m_new[i]
                                                    d_smem[row] = d_new[i]
                                                    m_prev_smem[row] = m_prev[i]
                                        T.tvm_storage_sync("shared")

                                        # Update O
                                        with T.block():
                                            for li, lj, lk in T.grid(tile_x, d_v, tile_z):
                                                with T.block("O_gemm"):
                                                    i, j, k = T.axis.remap("SSR", [li, lj, lk])
                                                    with T.init():
                                                        O_local[i, j] *= T.exp2(m_prev_smem[i] - m_smem[i])
                                                    O_local[i, j] += S_smem[i, k] * T.cast(V_smem[k, j], "float32")

                                    # Store O from smem to gmem
                                    for li, lj in T.grid(tile_x, d_v):
                                        with T.block("O_store"):
                                            i, j = T.axis.remap("SS", [li, lj])
                                            cur_L: T.int32 = q_indptr[b_idx] + (LH_start + i) // group_size
                                            cur_H_qo: T.int32 = by * group_size + (LH_start + i) % group_size
                                            if cur_L < q_indptr[b_idx + 1]:
                                                output[cur_L, cur_H_qo, j] = O_local[i, j] / d_smem[i]

                                    # Store LSE to gmem
                                    for li in T.grid(tile_x):
                                        with T.block("lse_store"):
                                            i = T.axis.remap("S", [li])
                                            cur_L: T.int32 = q_indptr[b_idx] + (LH_start + i) // group_size
                                            cur_H_qo: T.int32 = by * group_size + (LH_start + i) % group_size
                                            if cur_L < q_indptr[b_idx + 1]:
                                                lse[cur_L, cur_H_qo] = m_smem[i] + T.log2(d_smem[i])

                                    # move to next tile
                                    tile_id[0] += NUM_BLKS
    # fmt: on
    # pylint: enable=line-too-long,too-many-branches
    sch = tir.Schedule(batch_prefill_ragged_kv)
    sch = _schedule_prefill_kernel(sch, LOAD_VEC, bdx, num_warps, tile_x, d_v, tile_z, True, False)
    return sch.mod["main"].with_attr("tir.is_scheduled", True)


def _attention_prefill_mla(
    h_q,
    d_latent,
    d_rope,
    dtype,
    sliding_window: bool,
    target: Target,
    page_size: int = 16,
):
    d_qk = d_latent + d_rope
    (
        NUM_BLKS,
        LOAD_VEC,
        group_size,
        bdx,
        num_warps,
        tile_x,
        tile_y,
        tile_z,
    ) = _get_prefill_kernel_config(1, h_q, d_qk, dtype, target)

    global_symbol = "batch_prefill_paged_kv_mla"
    if sliding_window:
        global_symbol += "_sliding_window"

    # pylint: disable=line-too-long,too-many-branches
    # fmt: off
    @T.prim_func
    def batch_prefill_paged_kv_mla(
        var_q: T.handle, # [total_len, h_q, d_qk]
        var_q_indptr: T.handle, # [batch_size + 1]
        var_pages: T.handle, # [max_num_pages, page_size, d_qk]
        var_page_indptr: T.handle, # [batch_size + 1]
        var_page_values: T.handle, # [nnz_pages]
        var_length_info: T.handle, # [b] when sliding window = False, or otherwise [3, b]
        var_output: T.handle, # [total_len, h_q, d_latent]
        var_lse: T.handle, # [total_len, h_q]
        causal: T.int32,
        sm_scale: T.float32,
    ):
        T.func_attr({"global_symbol": global_symbol})
        batch_size = T.int32(is_size_var=True)
        total_len = T.int32(is_size_var=True)
        nnz_pages = T.int32(is_size_var=True)
        max_num_pages = T.int32(is_size_var=True)
        pages_elem_offset = T.int64(is_size_var=True)
        q_indptr_elem_offset = T.int32(is_size_var=True)
        page_indptr_elem_offset = T.int32(is_size_var=True)
        page_values_elem_offset = T.int32(is_size_var=True)
        length_info_elem_offset = T.int32(is_size_var=True)

        q = T.match_buffer(var_q, (total_len, h_q, d_qk), dtype)
        q_indptr = T.match_buffer(var_q_indptr, (batch_size + 1,), "int32", elem_offset=q_indptr_elem_offset)
        pages = T.match_buffer(var_pages, (max_num_pages, page_size, d_qk), dtype, elem_offset=pages_elem_offset)
        page_indptr = T.match_buffer(var_page_indptr, (batch_size + 1,), "int32", elem_offset=page_indptr_elem_offset)
        page_values = T.match_buffer(var_page_values, (nnz_pages,), "int32", elem_offset=page_values_elem_offset)
        output = T.match_buffer(var_output, (total_len, h_q, d_latent), dtype)
        lse = T.match_buffer(var_lse, (total_len, h_q), "float32")  # pylint: disable=unused-variable
        # The length information of the sequences.
        # - It is in shape `(3, batch_size)` when sliding window is enabled.
        #   For a sequence "i", location
        #   - "(0, i)" is the number of KV slots used in the last page of the seq ("last_page_len"),
        #   - "(1, i)" is the starting offset of the sliding window in the seq,
        #   - "(2, i)" is the attn sink length of the sequence.
        # - It is in shape `(batch_size,)` when sliding window is disabled,
        #   denoting the "last_page_len".
        length_info = _declare_length_info(var_length_info, batch_size, sliding_window, length_info_elem_offset)

        # kernel code
        for lbx in T.thread_binding(NUM_BLKS, thread="blockIdx.x"):
            for lty in T.thread_binding(num_warps, thread="threadIdx.y"):
                for ltx in T.thread_binding(bdx, thread="threadIdx.x"):
                    with T.block("attn"):
                        bx, ty, tx = T.axis.remap("SSS", [lbx, lty, ltx])
                        T.reads()
                        T.writes()
                        tile_id = _var("int32")
                        batch_idx = _var("int32")
                        batch_tiles = _var("int32")
                        batch_rows = _var("int32")
                        iterator = _var("int32")
                        kv_chunk_len = _var("int32")

                        Q_smem = T.alloc_buffer((tile_x, d_qk), dtype, scope="shared")
                        KV_smem = T.alloc_buffer((tile_z, d_qk), dtype, scope="shared")
                        S_smem = T.alloc_buffer((tile_x, tile_z), "float32", scope="shared")

                        S_local = T.alloc_buffer((tile_x, tile_z), "float32", scope="local")
                        O_local = T.alloc_buffer((tile_x, d_latent), "float32", scope="local")

                        m_smem = T.alloc_buffer((tile_x, ), "float32", scope="shared")
                        m_prev_smem = T.alloc_buffer((tile_x, ), "float32", scope="shared")
                        d_smem = T.alloc_buffer((tile_x, ), "float32", scope="shared")

                        m_new = T.alloc_buffer((math.ceil(tile_x / (bdx * num_warps)),), "float32", scope="local")
                        m_prev = T.alloc_buffer((math.ceil(tile_x / (bdx * num_warps)),), "float32", scope="local")
                        d_new = T.alloc_buffer((math.ceil(tile_x / (bdx * num_warps)),), "float32", scope="local")

                        ## get tile_no, batch_idx, batch_tiles, batch_rows
                        tile_id[0] = bx
                        batch_idx[0] = 0
                        batch_rows[0] = (q_indptr[1] - q_indptr[0]) * group_size
                        batch_tiles[0] = T.ceildiv(batch_rows[0], tile_x)
                        while T.tvm_thread_invariant(batch_idx[0] < batch_size):
                            # advance to next tile
                            while tile_id[0] >= batch_tiles[0] and batch_idx[0] < batch_size:
                                tile_id[0] -= batch_tiles[0]
                                batch_idx[0] += 1
                                if batch_idx[0] < batch_size:
                                    b_idx: T.int32 = batch_idx[0]
                                    batch_rows[0] = (q_indptr[b_idx + 1] - q_indptr[b_idx]) * group_size
                                    batch_tiles[0] = T.ceildiv(batch_rows[0], tile_x)

                            if T.tvm_thread_invariant(batch_idx[0] < batch_size):
                                b_idx: T.int32 = batch_idx[0]
                                LH_start: T.int32 = tile_id[0] * tile_x
                                q_indptr_val: T.int32 = q_indptr[b_idx]

                                cur_page_indptr_begin: T.int32 = page_indptr[b_idx]
                                cur_page_indptr_end: T.int32 = page_indptr[b_idx + 1]
                                kv_chunk_len[0] = T.if_then_else(
                                    cur_page_indptr_begin != cur_page_indptr_end,
                                    _get_kv_chunk_len(cur_page_indptr_end - cur_page_indptr_begin, page_size, b_idx, length_info, sliding_window),
                                    0
                                )
                                T.tvm_storage_sync("shared")

                                # init states
                                for i in T.serial(T.ceildiv(tile_x, bdx * num_warps)):
                                    row: T.int32 = i * bdx * num_warps + ty * bdx + tx
                                    if row < tile_x:
                                        m_smem[row] = -5e4
                                        d_smem[row] = 1.0

                                for li, lj in T.grid(tile_x, d_latent):
                                    with T.block("O_init"):
                                        i, j = T.axis.remap("SS", [li, lj])
                                        O_local[i, j] = 0.0
                                T.tvm_storage_sync("shared")

                                # Load Q from gmem to smem
                                for li, lj in T.grid(tile_x, tile_y):
                                    with T.block("Q_load"):
                                        i, j = T.axis.remap("SS", [li, lj])
                                        T.reads()
                                        T.writes()
                                        cur_L = q_indptr_val + (LH_start + i) // group_size
                                        cur_H_qo = (LH_start + i) % group_size
                                        if cur_L < q_indptr[b_idx + 1]:
                                            Q_smem[i, j] = q[cur_L, cur_H_qo, j]
                                        else:
                                            Q_smem[i, j] = 0.0
                                T.tvm_storage_sync("shared")

                                for iterator in T.serial(T.ceildiv(kv_chunk_len[0], tile_z)):
                                    L_kv_start: T.int32 = iterator * tile_z
                                    for lz, ly in T.grid(tile_z, tile_y):
                                        with T.block("KV_load"):
                                            i, j = T.axis.remap("SS", [lz, ly])
                                            T.reads()
                                            T.writes()
                                            cur_L = L_kv_start + i
                                            if cur_L < kv_chunk_len[0]:
                                                seq_offset: T.int32(is_size_var=True) = _get_seq_offset(cur_L, b_idx, length_info, sliding_window)  # type: ignore
                                                page_no: T.int32(is_size_var=True) = page_values[cur_page_indptr_begin + T.floordiv(seq_offset, page_size)]  # type: ignore
                                                page_offset: T.int32(is_size_var=True) = T.floormod(seq_offset, page_size)  # type: ignore
                                                KV_smem[i, j] = pages[page_no, page_offset, j]
                                            else:
                                                KV_smem[i, j] = 0.0
                                    T.tvm_storage_sync("shared")

                                    # Compute S
                                    with T.block():
                                        for li, lj, lk in T.grid(tile_x, tile_z, tile_y):
                                            with T.block("S_gemm"):
                                                i, j, k = T.axis.remap("SSR", [li, lj, lk])
                                                with T.init():
                                                    S_local[i, j] = 0.0
                                                S_local[i, j] += T.cast(Q_smem[i, k], "float32") * T.cast(KV_smem[j, k], "float32") * sm_scale * math.log2(math.exp(1))
                                    T.tvm_storage_sync("shared")
                                    for li, lj in T.grid(tile_x, tile_z):
                                        with T.block("S_store"):
                                            i, j = T.axis.remap("SS", [li, lj])
                                            S_smem[i, j] = S_local[i, j]
                                    T.tvm_storage_sync("shared")

                                    # Update S, m, d
                                    for i in T.serial(T.ceildiv(tile_x, bdx * num_warps)):
                                        row: T.int32 = i * bdx * num_warps + ty * bdx + tx
                                        if row < tile_x:
                                            with T.block("update1"):
                                                m_prev[i] = m_smem[row]
                                                m_new[i] = m_smem[row]
                                                # mask out of kv_chunk_len S
                                                row_: T.int32 = (LH_start + row) // group_size
                                                for j in T.serial(tile_z):
                                                    if _causal_mask(causal,
                                                            row=row_,
                                                            col=L_kv_start + j,
                                                            kv_len=kv_chunk_len[0],
                                                            qo_len=q_indptr[b_idx + 1] - q_indptr[b_idx]):
                                                        m_new[i] = T.max(m_new[i], S_smem[row, j])
                                                d_new[i] = d_smem[row] * T.exp2(m_prev[i] - m_new[i])

                                    for i in T.serial(T.ceildiv(tile_x, bdx * num_warps)):
                                        row: T.int32 = i * bdx * num_warps + ty * bdx + tx
                                        with T.block("update"):
                                            for j in T.serial(tile_z):
                                                # this is to avoid sync inside condition branch
                                                if row < tile_x:
                                                    row_: T.int32 = (LH_start + row) // group_size
                                                    if _causal_mask(causal,
                                                            row=row_,
                                                            col=L_kv_start + j,
                                                            kv_len=kv_chunk_len[0],
                                                            qo_len=q_indptr[b_idx + 1] - q_indptr[b_idx]):
                                                        S_smem[row, j] = T.exp2(S_smem[row, j] - m_new[i])
                                                    else:
                                                        S_smem[row, j] = T.exp2(-5e4 - m_new[i])

                                    for i in T.serial(T.ceildiv(tile_x, bdx * num_warps)):
                                        row: T.int32 = i * bdx * num_warps + ty * bdx + tx
                                        if row < tile_x:
                                            with T.block("update"):
                                                for j in T.serial(tile_z):
                                                    d_new[i] += S_smem[row, j]
                                                m_smem[row] = m_new[i]
                                                d_smem[row] = d_new[i]
                                                m_prev_smem[row] = m_prev[i]
                                    T.tvm_storage_sync("shared")

                                    # Update O
                                    with T.block():
                                        for li, lj, lk in T.grid(tile_x, d_latent, tile_z):
                                            with T.block("O_gemm"):
                                                i, j, k = T.axis.remap("SSR", [li, lj, lk])
                                                with T.init():
                                                    O_local[i, j] *= T.exp2(m_prev_smem[i] - m_smem[i])
                                                O_local[i, j] += S_smem[i, k] * T.cast(KV_smem[k, j], "float32")

                                # Store O from smem to gmem
                                for li, lj in T.grid(tile_x, d_latent):
                                    with T.block("O_store"):
                                        i, j = T.axis.remap("SS", [li, lj])
                                        cur_L: T.int32 = q_indptr[b_idx] + (LH_start + i) // group_size
                                        cur_H_qo: T.int32 = (LH_start + i) % group_size
                                        if cur_L < q_indptr[b_idx + 1]:
                                            output[cur_L, cur_H_qo, j] = O_local[i, j] / d_smem[i]

                                # Store LSE to gmem
                                for li in T.grid(tile_x):
                                    with T.block("lse_store"):
                                        i = T.axis.remap("S", [li])
                                        cur_L: T.int32 = q_indptr[b_idx] + (LH_start + i) // group_size
                                        cur_H_qo: T.int32 = (LH_start + i) % group_size
                                        if cur_L < q_indptr[b_idx + 1]:
                                            lse[cur_L, cur_H_qo] = m_smem[i] + T.log2(d_smem[i])

                                # move to next tile
                                tile_id[0] += NUM_BLKS
    # fmt: on
    # pylint: enable=line-too-long,too-many-branches
    sch = tir.Schedule(batch_prefill_paged_kv_mla)
    sch = _schedule_prefill_kernel(
        sch, LOAD_VEC, bdx, num_warps, tile_x, d_latent, tile_z, False, True
    )
    return sch.mod["main"].with_attr("tir.is_scheduled", True)


def _copy_single_page(num_heads, page_size, head_dim, dtype, target: Target):
    tx = get_max_num_threads_per_block(target)

    @T.prim_func
    def copy_single_page(
        var_pages: T.handle,
        src_page_id: T.int64,
        tgt_page_id: T.int64,
        copy_length: T.int64,
    ):
        T.func_attr({"tir.is_scheduled": True})
        num_pages = T.int32()
        pages_elem_offset = T.int64()
        pages = T.match_buffer(
            var_pages,
            (num_pages, 2, num_heads, page_size, head_dim),
            dtype,
            elem_offset=pages_elem_offset,
        )

        for b in T.thread_binding(
            (copy_length * num_heads * head_dim + tx - 1) // tx, thread="blockIdx.x"
        ):
            for t in T.thread_binding(tx, thread="threadIdx.x"):
                with T.block("copy"):
                    T.where(b * tx + t < copy_length * num_heads * head_dim)
                    vh = T.axis.spatial(
                        num_heads,
                        T.Cast("int32", (b * tx + t) // (copy_length * head_dim)),
                    )
                    vp = T.axis.spatial(
                        copy_length,
                        (b * tx + t) % (copy_length * head_dim) // head_dim,
                    )
                    vd = T.axis.spatial(
                        head_dim,
                        T.Cast(
                            "int32",
                            (b * tx + t) % head_dim,
                        ),
                    )
                    pages[tgt_page_id, 0, vh, vp, vd] = pages[src_page_id, 0, vh, vp, vd]
                    pages[tgt_page_id, 1, vh, vp, vd] = pages[src_page_id, 1, vh, vp, vd]

    return copy_single_page


def _copy_single_page_mla(page_size, head_dim, dtype, target: Target):
    tx = get_max_num_threads_per_block(target)

    @T.prim_func
    def copy_single_page_mla(
        var_pages: T.handle,
        src_page_id: T.int64,
        tgt_page_id: T.int64,
        copy_length: T.int64,
    ):
        T.func_attr({"tir.is_scheduled": True})
        num_pages = T.int32()
        pages_elem_offset = T.int64()
        pages = T.match_buffer(
            var_pages, (num_pages, page_size, head_dim), dtype, elem_offset=pages_elem_offset
        )

        for b in T.thread_binding((copy_length * head_dim + tx - 1) // tx, thread="blockIdx.x"):
            for t in T.thread_binding(tx, thread="threadIdx.x"):
                with T.block("copy"):
                    T.where(b * tx + t < copy_length * head_dim)
                    vp = T.axis.spatial(copy_length, (b * tx + t) // head_dim)
                    vd = T.axis.spatial(head_dim, T.Cast("int32", (b * tx + t) % head_dim))
                    pages[tgt_page_id, vp, vd] = pages[src_page_id, vp, vd]

    return copy_single_page_mla


def _copy_single_page_cpu(num_heads, page_size, head_dim, dtype):
    tx = 1

    @T.prim_func
    def copy_single_page_cpu(
        var_pages: T.handle,
        src_page_id: T.int64,
        tgt_page_id: T.int64,
        copy_length: T.int64,
    ):
        T.func_attr({"tir.is_scheduled": True})
        num_pages = T.int32()
        pages = T.match_buffer(var_pages, (num_pages, 2, num_heads, page_size, head_dim), dtype)

        for b in T.serial((copy_length * num_heads * head_dim + tx - 1) // tx):
            for t in T.serial(tx):
                with T.block("copy"):
                    T.where(b * tx + t < copy_length * num_heads * head_dim)
                    vh = T.axis.spatial(
                        num_heads,
                        T.Cast("int32", (b * tx + t) // (copy_length * head_dim)),
                    )
                    vp = T.axis.spatial(
                        copy_length,
                        (b * tx + t) % (copy_length * head_dim) // head_dim,
                    )
                    vd = T.axis.spatial(
                        head_dim,
                        T.Cast(
                            "int32",
                            (b * tx + t) % head_dim,
                        ),
                    )
                    pages[tgt_page_id, 0, vh, vp, vd] = pages[src_page_id, 0, vh, vp, vd]
                    pages[tgt_page_id, 1, vh, vp, vd] = pages[src_page_id, 1, vh, vp, vd]

    return copy_single_page_cpu


def _compact_kv_copy(num_heads, head_dim, dtype, target: Target, page_size: int = 16):
    tx = get_max_num_threads_per_block(target)

    @T.prim_func
    def compact_kv_copy(
        var_pages: T.handle,
        var_copy_length_indptr: T.handle,
        var_copy_src_dst_pos: T.handle,
        batch_size: T.int32,
    ):
        T.func_attr({"tir.is_scheduled": True})
        num_pages = T.int32()
        total_copy_length = T.int32()
        copy_length_indptr_elem_offset = T.int32()
        copy_src_dst_pos_elem_offset = T.int32()
        pages_elem_offset = T.int64()
        pages = T.match_buffer(
            var_pages,
            (num_pages, 2, num_heads, page_size, head_dim),
            dtype,
            elem_offset=pages_elem_offset,
        )
        copy_length_indptr = T.match_buffer(
            var_copy_length_indptr,
            (batch_size + 1,),
            "int32",
            elem_offset=copy_length_indptr_elem_offset,
        )
        copy_src_dst_pos = T.match_buffer(
            var_copy_src_dst_pos,
            (2, total_copy_length),
            "int32",
            elem_offset=copy_src_dst_pos_elem_offset,
        )

        with T.block("root"):
            for bhd_o in T.thread_binding(
                (batch_size * num_heads * head_dim + tx - 1) // tx, thread="blockIdx.x"
            ):
                for bhd_i in T.thread_binding(tx, thread="threadIdx.x"):
                    b: T.int32 = (bhd_o * tx + bhd_i) // (num_heads * head_dim)
                    h: T.int32 = (bhd_o * tx + bhd_i) // head_dim % num_heads
                    d: T.int32 = (bhd_o * tx + bhd_i) % head_dim
                    if (bhd_o * tx + bhd_i) < batch_size * num_heads * head_dim:
                        for i in T.serial(copy_length_indptr[b + 1] - copy_length_indptr[b]):
                            src_pos: T.int32 = copy_src_dst_pos[0, copy_length_indptr[b] + i]
                            dst_pos: T.int32 = copy_src_dst_pos[1, copy_length_indptr[b] + i]
                            pages[dst_pos // page_size, 0, h, dst_pos % page_size, d] = pages[
                                src_pos // page_size, 0, h, src_pos % page_size, d
                            ]
                            pages[dst_pos // page_size, 1, h, dst_pos % page_size, d] = pages[
                                src_pos // page_size, 1, h, src_pos % page_size, d
                            ]

    return compact_kv_copy


def _compact_kv_copy_cpu(num_heads, head_dim, dtype, page_size: int = 16):
    tx = 8

    @T.prim_func
    def compact_kv_copy_cpu(
        var_pages: T.handle,
        var_copy_length_indptr: T.handle,
        var_copy_src_dst_pos: T.handle,
        batch_size: T.int32,
    ):
        T.func_attr({"tir.is_scheduled": True})
        num_pages = T.int32()
        total_copy_length = T.int32()
        copy_length_indptr_elem_offset = T.int32()
        copy_src_dst_pos_elem_offset = T.int32()
        pages = T.match_buffer(var_pages, (num_pages, 2, num_heads, page_size, head_dim), dtype)
        copy_length_indptr = T.match_buffer(
            var_copy_length_indptr,
            (batch_size + 1,),
            "int32",
            elem_offset=copy_length_indptr_elem_offset,
        )
        copy_src_dst_pos = T.match_buffer(
            var_copy_src_dst_pos,
            (2, total_copy_length),
            "int32",
            elem_offset=copy_src_dst_pos_elem_offset,
        )

        with T.block("root"):
            for bhd_o in T.serial((batch_size * num_heads * head_dim + tx - 1) // tx):
                for bhd_i in T.serial(tx):
                    b: T.int32 = (bhd_o * tx + bhd_i) // (num_heads * head_dim)
                    h: T.int32 = (bhd_o * tx + bhd_i) // head_dim % num_heads
                    d: T.int32 = (bhd_o * tx + bhd_i) % head_dim
                    if (bhd_o * tx + bhd_i) < batch_size * num_heads * head_dim:
                        for i in T.serial(copy_length_indptr[b + 1] - copy_length_indptr[b]):
                            src_pos: T.int32 = copy_src_dst_pos[0, copy_length_indptr[b] + i]
                            dst_pos: T.int32 = copy_src_dst_pos[1, copy_length_indptr[b] + i]
                            pages[dst_pos // page_size, 0, h, dst_pos % page_size, d] = pages[
                                src_pos // page_size, 0, h, src_pos % page_size, d
                            ]
                            pages[dst_pos // page_size, 1, h, dst_pos % page_size, d] = pages[
                                src_pos // page_size, 1, h, src_pos % page_size, d
                            ]

    return compact_kv_copy_cpu
