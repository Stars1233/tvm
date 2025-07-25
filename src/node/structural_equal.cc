/*
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied.  See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */
/*!
 * \file src/node/structural_equal.cc
 */
#include <tvm/ffi/function.h>
#include <tvm/ffi/reflection/registry.h>
#include <tvm/ir/module.h>
#include <tvm/node/functor.h>
#include <tvm/node/node.h>
#include <tvm/node/object_path.h>
#include <tvm/node/reflection.h>
#include <tvm/node/structural_equal.h>

#include <optional>
#include <unordered_map>

#include "ndarray_hash_equal.h"

namespace tvm {

TVM_REGISTER_OBJECT_TYPE(ObjectPathPairNode);

TVM_FFI_STATIC_INIT_BLOCK({
  namespace refl = tvm::ffi::reflection;
  refl::GlobalDef()
      .def("node.ObjectPathPairLhsPath",
           [](const ObjectPathPair& object_path_pair) { return object_path_pair->lhs_path; })
      .def("node.ObjectPathPairRhsPath",
           [](const ObjectPathPair& object_path_pair) { return object_path_pair->rhs_path; });
});

ObjectPathPairNode::ObjectPathPairNode(ObjectPath lhs_path, ObjectPath rhs_path)
    : lhs_path(std::move(lhs_path)), rhs_path(std::move(rhs_path)) {}

ObjectPathPair::ObjectPathPair(ObjectPath lhs_path, ObjectPath rhs_path) {
  data_ = make_object<ObjectPathPairNode>(std::move(lhs_path), std::move(rhs_path));
}

// Define the dispatch function here since primary user is in this file.
bool ReflectionVTable::SEqualReduce(const Object* self, const Object* other,
                                    SEqualReducer equal) const {
  uint32_t tindex = self->type_index();
  if (tindex >= fsequal_reduce_.size() || fsequal_reduce_[tindex] == nullptr) {
    LOG(FATAL) << "TypeError: SEqualReduce of " << self->GetTypeKey()
               << " is not registered via TVM_REGISTER_NODE_TYPE."
               << " Did you forget to set _type_has_method_sequal_reduce=true?";
  }
  return fsequal_reduce_[tindex](self, other, equal);
}

namespace {
ObjectPath GetAttrPath(const ObjectRef& obj, const void* attr_address, const ObjectPath& path) {
  Optional<String> attr_key = GetAttrKeyByAddress(obj.get(), attr_address);
  return path->Attr(attr_key);
}
}  // namespace

struct SEqualReducer::PathTracingData {
  ObjectPathPair current_paths;
  ObjectRef lhs_object;
  ObjectRef rhs_object;
  Optional<ObjectPathPair>* first_mismatch;

  ObjectPathPair GetPathsForAttrs(const ObjectRef& lhs, const ObjectRef& rhs) const {
    ObjectPath lhs_attr_path = GetAttrPath(lhs_object, &lhs, current_paths->lhs_path);
    ObjectPath rhs_attr_path = GetAttrPath(rhs_object, &rhs, current_paths->rhs_path);
    return ObjectPathPair(lhs_attr_path, rhs_attr_path);
  }
};

bool SEqualReducer::operator()(const ObjectRef& lhs, const ObjectRef& rhs) const {
  if (tracing_data_ == nullptr) {
    // Fast path: no tracing
    return handler_->SEqualReduce(lhs, rhs, map_free_vars_, std::nullopt);
  }
  return ObjectAttrsEqual(lhs, rhs, map_free_vars_, nullptr);
}

bool SEqualReducer::DefEqual(const ObjectRef& lhs, const ObjectRef& rhs) {
  if (tracing_data_ == nullptr) {
    // Fast path: no tracing
    return handler_->SEqualReduce(lhs, rhs, true, std::nullopt);
  }
  return ObjectAttrsEqual(lhs, rhs, true, nullptr);
}

/* static */ void SEqualReducer::GetPathsFromAttrAddressesAndStoreMismatch(
    const void* lhs_address, const void* rhs_address, const PathTracingData* tracing_data) {
  if (tracing_data != nullptr && !tracing_data->first_mismatch->defined()) {
    ObjectPath lhs_attr_path =
        GetAttrPath(tracing_data->lhs_object, lhs_address, tracing_data->current_paths->lhs_path);
    ObjectPath rhs_attr_path =
        GetAttrPath(tracing_data->rhs_object, rhs_address, tracing_data->current_paths->rhs_path);

    *tracing_data->first_mismatch = ObjectPathPair(lhs_attr_path, rhs_attr_path);
  }
}

template <typename T>
/* static */ bool SEqualReducer::CompareAttributeValues(const T& lhs, const T& rhs,
                                                        const PathTracingData* tracing_data,
                                                        Optional<ObjectPathPair> paths) {
  if (BaseValueEqual()(lhs, rhs)) {
    return true;
  }

  if (tracing_data && !tracing_data->first_mismatch->defined()) {
    if (paths) {
      *tracing_data->first_mismatch = paths.value();
    } else {
      GetPathsFromAttrAddressesAndStoreMismatch(&lhs, &rhs, tracing_data);
    }
  }
  return false;
}

bool SEqualReducer::operator()(const double& lhs, const double& rhs,
                               Optional<ObjectPathPair> paths) const {
  return CompareAttributeValues(lhs, rhs, tracing_data_, paths);
}

bool SEqualReducer::operator()(const int64_t& lhs, const int64_t& rhs,
                               Optional<ObjectPathPair> paths) const {
  return CompareAttributeValues(lhs, rhs, tracing_data_, paths);
}

bool SEqualReducer::operator()(const Optional<double>& lhs, const Optional<double>& rhs,
                               Optional<ObjectPathPair> paths) const {
  return CompareAttributeValues(lhs, rhs, tracing_data_, paths);
}

bool SEqualReducer::operator()(const Optional<int64_t>& lhs, const Optional<int64_t>& rhs,
                               Optional<ObjectPathPair> paths) const {
  return CompareAttributeValues(lhs, rhs, tracing_data_, paths);
}

bool SEqualReducer::operator()(const uint64_t& lhs, const uint64_t& rhs,
                               Optional<ObjectPathPair> paths) const {
  return CompareAttributeValues(lhs, rhs, tracing_data_, paths);
}

bool SEqualReducer::operator()(const int& lhs, const int& rhs,
                               Optional<ObjectPathPair> paths) const {
  return CompareAttributeValues(lhs, rhs, tracing_data_, paths);
}

bool SEqualReducer::operator()(const bool& lhs, const bool& rhs,
                               Optional<ObjectPathPair> paths) const {
  return CompareAttributeValues(lhs, rhs, tracing_data_, paths);
}

bool SEqualReducer::operator()(const std::string& lhs, const std::string& rhs,
                               Optional<ObjectPathPair> paths) const {
  return CompareAttributeValues(lhs, rhs, tracing_data_, paths);
}

bool SEqualReducer::operator()(const DataType& lhs, const DataType& rhs,
                               Optional<ObjectPathPair> paths) const {
  return CompareAttributeValues(lhs, rhs, tracing_data_, paths);
}

bool SEqualReducer::AnyEqual(const ffi::Any& lhs, const ffi::Any& rhs,
                             Optional<ObjectPathPair> paths) const {
  auto record_mismatch = [&]() {
    if (tracing_data_ && !tracing_data_->first_mismatch->defined()) {
      if (paths) {
        *tracing_data_->first_mismatch = paths.value();
      }
    }
  };
  if (lhs.type_index() != rhs.type_index()) {
    record_mismatch();
    return false;
  }
  if (lhs.type_index() >= ffi::TypeIndex::kTVMFFIStaticObjectBegin) {
    if (paths) {
      return operator()(lhs.cast<ObjectRef>(), rhs.cast<ObjectRef>(), paths.value());
    } else {
      ObjectRef lhs_obj = lhs.cast<ObjectRef>();
      ObjectRef rhs_obj = rhs.cast<ObjectRef>();
      bool result = operator()(lhs_obj, rhs_obj);
      return result;
    }
  }

  if (ffi::details::AnyUnsafe::TVMFFIAnyPtrFromAny(lhs)->v_uint64 ==
      ffi::details::AnyUnsafe::TVMFFIAnyPtrFromAny(rhs)->v_uint64) {
    return true;
  }
  record_mismatch();
  return false;
}

bool SEqualReducer::EnumAttrsEqual(int lhs, int rhs, const void* lhs_address,
                                   const void* rhs_address, Optional<ObjectPathPair> paths) const {
  if (lhs == rhs) {
    return true;
  }

  if (tracing_data_ && !tracing_data_->first_mismatch->defined()) {
    if (paths) {
      *tracing_data_->first_mismatch = paths.value();
    } else {
      GetPathsFromAttrAddressesAndStoreMismatch(&lhs, &rhs, tracing_data_);
    }
  }

  return false;
}

const ObjectPathPair& SEqualReducer::GetCurrentObjectPaths() const {
  ICHECK(tracing_data_ != nullptr)
      << "GetCurrentObjectPaths() can only be called when path tracing is enabled";
  return tracing_data_->current_paths;
}

void SEqualReducer::RecordMismatchPaths(const ObjectPathPair& paths) const {
  ICHECK(tracing_data_ != nullptr)
      << "RecordMismatchPaths() can only be called when path tracing is enabled";
  if (!tracing_data_->first_mismatch->defined()) {
    *tracing_data_->first_mismatch = paths;
  }
}

bool SEqualReducer::ObjectAttrsEqual(const ObjectRef& lhs, const ObjectRef& rhs, bool map_free_vars,
                                     const ObjectPathPair* paths) const {
  if (tracing_data_ == nullptr) {
    // Fast path: no tracing
    return handler_->SEqualReduce(lhs, rhs, map_free_vars, std::nullopt);
  }

  // Slow path: tracing object paths for better error reporting
  ObjectPathPair new_paths = paths == nullptr ? tracing_data_->GetPathsForAttrs(lhs, rhs) : *paths;

  if (handler_->SEqualReduce(lhs, rhs, map_free_vars, new_paths)) {
    return true;
  } else {
    if (!tracing_data_->first_mismatch->defined()) {
      *tracing_data_->first_mismatch = new_paths;
    }
    return false;
  }
}

/*!
 * \brief A non recursive stack based SEqual handler that can remaps vars.
 *
 *  This handler pushs the Object equality cases into a stack, and
 *  traverses the stack to expand the necessary children that need to be checked.
 *
 *  The order of SEqual being called is the same as the order as if we
 *  eagerly do recursive calls in SEqualReduce.
 */
class SEqualHandlerDefault::Impl {
 public:
  Impl(SEqualHandlerDefault* parent, bool assert_mode, Optional<ObjectPathPair>* first_mismatch,
       bool defer_fails)
      : parent_(parent),
        assert_mode_(assert_mode),
        first_mismatch_(first_mismatch),
        defer_fails_(defer_fails) {}

  bool SEqualReduce(const ObjectRef& lhs, const ObjectRef& rhs, bool map_free_vars,
                    const Optional<ObjectPathPair>& current_paths) {
    // We cannot use check lhs.same_as(rhs) to check equality.
    // if we choose to enable var remapping.
    //
    // Counter example below (%x, %y) are shared vars
    // between the two functions(possibly before/after rewriting).
    //
    // - function0: fn (%x, %y) { %x + %y }
    // - function1. fn (%y, %x) { %x + %y }
    //
    // Because we choose to enable var remapping,
    // %x is mapped to %y, and %y is mapped to %x,
    // the body of the function no longer means the same thing.
    //
    // Take away: We can either choose only compare Var by address,
    // in which case we can use same_as for quick checking,
    // or we have to run deep comparison and avoid to use same_as checks.
    auto run = [=]() {
      std::optional<bool> early_result = [&]() -> std::optional<bool> {
        if (!lhs.defined() && !rhs.defined()) return true;
        if (!lhs.defined() && rhs.defined()) return false;
        if (!rhs.defined() && lhs.defined()) return false;
        if (lhs->type_index() != rhs->type_index()) return false;
        auto it = equal_map_lhs_.find(lhs);
        if (it != equal_map_lhs_.end()) {
          return it->second.same_as(rhs);
        }
        if (equal_map_rhs_.count(rhs)) return false;

        return std::nullopt;
      }();

      if (early_result.has_value()) {
        if (early_result.value()) {
          return true;
        } else if (IsPathTracingEnabled() && IsFailDeferralEnabled() && current_paths.defined()) {
          DeferFail(current_paths.value());
          return true;
        } else {
          return false;
        }
      }

      // need to push to pending tasks in this case
      pending_tasks_.emplace_back(lhs, rhs, map_free_vars, current_paths);
      return true;
    };
    return CheckResult(run(), lhs, rhs, current_paths);
  }

  void DeferFail(const ObjectPathPair& mismatch_paths) {
    pending_tasks_.emplace_back(Task::ForceFailTag{}, mismatch_paths);
  }

  bool IsFailDeferralEnabled() { return defer_fails_; }

  void MarkGraphNode() {
    // need to push to pending tasks in this case
    ICHECK(!allow_push_to_stack_ && !task_stack_.empty());
    task_stack_.back().graph_equal = true;
  }

  ObjectRef MapLhsToRhs(const ObjectRef& lhs) {
    auto it = equal_map_lhs_.find(lhs);
    if (it != equal_map_lhs_.end()) return it->second;
    return lhs;
  }

  // Function that implements actual equality check.
  bool Equal(const ffi::Any& lhs, const ffi::Any& rhs, bool map_free_vars) {
    task_stack_.clear();
    pending_tasks_.clear();
    equal_map_lhs_.clear();
    equal_map_rhs_.clear();
    root_lhs_ = lhs;
    root_rhs_ = rhs;
    Optional<ObjectPathPair> current_paths;
    if (IsPathTracingEnabled()) {
      auto root_path = ObjectPath::Root();
      current_paths = ObjectPathPair(root_path, root_path);
    }
    if (lhs.type_index() != rhs.type_index()) {
      return CheckResult(false, lhs, rhs, current_paths);
    }

    if (lhs.type_index() < ffi::TypeIndex::kTVMFFIStaticObjectBegin) {
      if (ffi::details::AnyUnsafe::TVMFFIAnyPtrFromAny(lhs)->v_uint64 ==
          ffi::details::AnyUnsafe::TVMFFIAnyPtrFromAny(rhs)->v_uint64) {
        return true;
      }
      return CheckResult(false, lhs, rhs, current_paths);
    }

    // normal object ref path
    if (!SEqualReduce(lhs.cast<ObjectRef>(), rhs.cast<ObjectRef>(), map_free_vars, current_paths)) {
      return false;
    }

    ICHECK_EQ(pending_tasks_.size(), 1U);
    ICHECK(allow_push_to_stack_);
    task_stack_.emplace_back(std::move(pending_tasks_.back()));
    pending_tasks_.clear();
    return RunTasks();
  }

  // The default equal as registered in the structural equal vtable.
  bool DispatchSEqualReduce(const ObjectRef& lhs, const ObjectRef& rhs, bool map_free_vars,
                            const Optional<ObjectPathPair>& current_paths) {
    auto compute = [=]() {
      ICHECK(lhs.defined() && rhs.defined() && lhs->type_index() == rhs->type_index());
      // skip entries that already have equality maps.
      auto it = equal_map_lhs_.find(lhs);
      if (it != equal_map_lhs_.end()) {
        return it->second.same_as(rhs);
      }
      if (equal_map_rhs_.count(rhs)) return false;

      if (!IsPathTracingEnabled()) {
        return vtable_->SEqualReduce(lhs.get(), rhs.get(),
                                     SEqualReducer(parent_, nullptr, map_free_vars));
      } else {
        PathTracingData tracing_data = {current_paths.value(), lhs, rhs, first_mismatch_};
        return vtable_->SEqualReduce(lhs.get(), rhs.get(),
                                     SEqualReducer(parent_, &tracing_data, map_free_vars));
      }
    };
    return CheckResult(compute(), lhs, rhs, current_paths);
  }

 protected:
  // Check the result.
  bool CheckResult(bool result, const Any& lhs, const Any& rhs,
                   const Optional<ObjectPathPair>& current_paths) {
    if (IsPathTracingEnabled() && !result && !first_mismatch_->defined()) {
      *first_mismatch_ = current_paths;
    }
    if (assert_mode_ && !result) {
      std::ostringstream oss;
      oss << "ValueError: StructuralEqual check failed, caused by lhs";
      if (first_mismatch_->defined()) {
        oss << " at " << first_mismatch_->value()->lhs_path;
        if (root_lhs_.has_value()) {
          PrinterConfig cfg;
          cfg->syntax_sugar = false;
          cfg->path_to_underline.push_back(first_mismatch_->value()->lhs_path);
          // The TVMScriptPrinter::Script will fallback to Repr printer,
          // if the root node to print is not supported yet,
          // e.g. Relax nodes, ArrayObj, MapObj, etc.
          oss << ":" << std::endl
              << TVMScriptPrinter::Script(root_lhs_.value().cast<ObjectRef>(), cfg);
        }
      } else {
        oss << ":" << std::endl << lhs;
      }
      oss << std::endl << "and rhs";
      if (first_mismatch_->defined()) {
        oss << " at " << first_mismatch_->value()->rhs_path;
        if (root_rhs_.has_value()) {
          PrinterConfig cfg;
          cfg->syntax_sugar = false;
          cfg->path_to_underline.push_back(first_mismatch_->value()->rhs_path);
          // The TVMScriptPrinter::Script will fallback to Repr printer,
          // if the root node to print is not supported yet,
          // e.g. Relax nodes, ArrayObj, MapObj, etc.
          oss << ":" << std::endl
              << TVMScriptPrinter::Script(root_rhs_.value().cast<ObjectRef>(), cfg);
        }
      } else {
        oss << ":" << std::endl << rhs;
      }
      LOG(FATAL) << oss.str();
    }
    return result;
  }
  /*!
   * \brief Run tasks until the stack reaches the stack begin
   * \param stack_begin The expected beginning of the stack.
   * \return The checks we encountered throughout the process.
   */
  bool RunTasks() {
    while (task_stack_.size() != 0) {
      // Caution: entry becomes invalid when the stack changes
      auto& entry = task_stack_.back();

      if (entry.force_fail) {
        return CheckResult(false, entry.lhs, entry.rhs, entry.current_paths);
      }

      if (entry.children_expanded) {
        // When all the children has expanded and visited.
        // This means all the condition checks for
        // the current entry has been passed
        // We can safely mark lhs and rhs as equal to each other.
        auto it = equal_map_lhs_.find(entry.lhs);
        if (it != equal_map_lhs_.end()) {
          ICHECK(it->second.same_as(entry.rhs));
        }
        // create the map if the quality is graph equal.
        if (entry.graph_equal) {
          equal_map_lhs_[entry.lhs] = entry.rhs;
          equal_map_rhs_[entry.rhs] = entry.lhs;
        }
        task_stack_.pop_back();
      } else {
        // mark before expand
        // Important: because entry becomes invalid when stack changes.
        entry.children_expanded = true;
        // Expand the objects
        // The SEqual of the object can call into this->SEqualReduce
        // which populates the pending tasks.
        ICHECK_EQ(pending_tasks_.size(), 0U);
        allow_push_to_stack_ = false;
        if (!parent_->DispatchSEqualReduce(entry.lhs, entry.rhs, entry.map_free_vars,
                                           entry.current_paths))
          return false;
        allow_push_to_stack_ = true;
        // Push pending tasks in reverse order, so earlier tasks get to
        // expand first in the stack
        while (pending_tasks_.size() != 0) {
          task_stack_.emplace_back(std::move(pending_tasks_.back()));
          pending_tasks_.pop_back();
        }
      }
    }
    return true;
  }

 private:
  /*! \brief Pending reduce tasks. */
  struct Task {
    /*! \brief The lhs operand to be compared. */
    ObjectRef lhs;
    /*! \brief The rhs operand to be compared. */
    ObjectRef rhs;
    /*! \brief If path tracing is enabled, paths taken so far from the root to `lhs` and `rhs`
     * objects. */
    Optional<ObjectPathPair> current_paths;
    /*! \brief The map free var argument. */
    bool map_free_vars;
    /*! \brief Whether the children has been expanded via SEqualReduce */
    bool children_expanded{false};
    /*! \brief whether the task is about graph equality(need remap). */
    bool graph_equal{false};
    /*! \brief whether the task should return "false" without actually comparing anything */
    bool force_fail{false};

    Task() = default;
    Task(ObjectRef lhs, ObjectRef rhs, bool map_free_vars, Optional<ObjectPathPair> current_paths)
        : lhs(lhs),
          rhs(rhs),
          current_paths(std::move(current_paths)),
          map_free_vars(map_free_vars) {}

    struct ForceFailTag {};  // dispatch tag for the constructor below
    Task(ForceFailTag, const ObjectPathPair& current_paths)
        : current_paths(current_paths), force_fail(true) {}
  };

  bool IsPathTracingEnabled() const { return first_mismatch_ != nullptr; }

  // The owner of this impl
  SEqualHandlerDefault* parent_;
  // list of pending tasks to be pushed to the stack.
  std::vector<Task> pending_tasks_;
  // Internal task stack to executed the task.
  std::vector<Task> task_stack_;
  // Whether we allow push to stack.
  bool allow_push_to_stack_{true};
  //  If in assert mode, must return true, and will throw error otherwise.
  bool assert_mode_{false};
  // Location to store the paths to the first detected mismatch, or nullptr to disable path
  // tracing.
  Optional<ObjectPathPair>* first_mismatch_;
  // reflection vtable
  ReflectionVTable* vtable_ = ReflectionVTable::Global();
  // map from lhs to rhs
  std::unordered_map<ObjectRef, ObjectRef, ObjectPtrHash, ObjectPtrEqual> equal_map_lhs_;
  // map from rhs to lhs
  std::unordered_map<ObjectRef, ObjectRef, ObjectPtrHash, ObjectPtrEqual> equal_map_rhs_;
  // root lhs for result printing
  Optional<Any> root_lhs_;
  // root rhs for result printing
  Optional<Any> root_rhs_;
  // whether to defer fails
  bool defer_fails_;
};

SEqualHandlerDefault::SEqualHandlerDefault(bool assert_mode,
                                           Optional<ObjectPathPair>* first_mismatch,
                                           bool defer_fails) {
  impl = new Impl(this, assert_mode, first_mismatch, defer_fails);
}

SEqualHandlerDefault::~SEqualHandlerDefault() { delete impl; }

bool SEqualHandlerDefault::SEqualReduce(const ObjectRef& lhs, const ObjectRef& rhs,
                                        bool map_free_vars,
                                        const Optional<ObjectPathPair>& current_paths) {
  return impl->SEqualReduce(lhs, rhs, map_free_vars, current_paths);
}

void SEqualHandlerDefault::DeferFail(const ObjectPathPair& mismatch_paths) {
  impl->DeferFail(mismatch_paths);
}

bool SEqualHandlerDefault::IsFailDeferralEnabled() { return impl->IsFailDeferralEnabled(); }

ObjectRef SEqualHandlerDefault::MapLhsToRhs(const ObjectRef& lhs) { return impl->MapLhsToRhs(lhs); }

void SEqualHandlerDefault::MarkGraphNode() { impl->MarkGraphNode(); }

bool SEqualHandlerDefault::Equal(const Any& lhs, const Any& rhs, bool map_free_vars) {
  return impl->Equal(lhs, rhs, map_free_vars);
}

bool SEqualHandlerDefault::DispatchSEqualReduce(const ObjectRef& lhs, const ObjectRef& rhs,
                                                bool map_free_vars,
                                                const Optional<ObjectPathPair>& current_paths) {
  return impl->DispatchSEqualReduce(lhs, rhs, map_free_vars, current_paths);
}

TVM_FFI_STATIC_INIT_BLOCK({
  namespace refl = tvm::ffi::reflection;
  refl::GlobalDef()
      .def("node.StructuralEqual",
           [](const Any& lhs, const Any& rhs, bool assert_mode, bool map_free_vars) {
             // If we are asserting on failure, then the `defer_fails` option
             // should be enabled, to provide better error messages.  For
             // example, if the number of bindings in a `relax::BindingBlock`
             // differs, highlighting the first difference rather than the
             // entire block.
             bool defer_fails = assert_mode;
             Optional<ObjectPathPair> first_mismatch;
             return SEqualHandlerDefault(assert_mode, &first_mismatch, defer_fails)
                 .Equal(lhs, rhs, map_free_vars);
           })
      .def("node.GetFirstStructuralMismatch",
           [](const Any& lhs, const Any& rhs, bool map_free_vars) {
             Optional<ObjectPathPair> first_mismatch;
             bool equal =
                 SEqualHandlerDefault(false, &first_mismatch, true).Equal(lhs, rhs, map_free_vars);
             ICHECK(equal == !first_mismatch.defined());
             return first_mismatch;
           });
});

bool StructuralEqual::operator()(const ObjectRef& lhs, const ObjectRef& rhs,
                                 bool map_free_params) const {
  return SEqualHandlerDefault(false, nullptr, false).Equal(lhs, rhs, map_free_params);
}

bool NDArrayEqual(const runtime::NDArray::Container* lhs, const runtime::NDArray::Container* rhs,
                  SEqualReducer equal, bool compare_data) {
  if (lhs == rhs) return true;

  auto ldt = lhs->dtype;
  auto rdt = rhs->dtype;
  ICHECK_EQ(lhs->device.device_type, kDLCPU) << "can only compare CPU tensor";
  ICHECK_EQ(rhs->device.device_type, kDLCPU) << "can only compare CPU tensor";
  ICHECK(runtime::IsContiguous(*lhs)) << "Can only compare contiguous tensor";
  ICHECK(runtime::IsContiguous(*rhs)) << "Can only compare contiguous tensor";

  if (lhs->ndim != rhs->ndim) return false;
  for (int i = 0; i < lhs->ndim; ++i) {
    if (!equal(lhs->shape[i], rhs->shape[i])) return false;
  }
  if (ldt.code == rdt.code && ldt.lanes == rdt.lanes && ldt.bits == rdt.bits) {
    size_t data_size = runtime::GetDataSize(*lhs);
    if (compare_data) {
      return std::memcmp(lhs->data, rhs->data, data_size) == 0;
    } else {
      return true;
    }
  } else {
    return false;
  }
}

bool NDArrayContainerTrait::SEqualReduce(const runtime::NDArray::Container* lhs,
                                         const runtime::NDArray::Container* rhs,
                                         SEqualReducer equal) {
  return NDArrayEqual(lhs, rhs, equal, true);
}

}  // namespace tvm
