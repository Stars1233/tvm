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
 * \file module_util.cc
 * \brief Utilities for module.
 */
#include "library_module.h"

#include <dmlc/memory_io.h>
#include <tvm/ffi/any.h>
#include <tvm/ffi/function.h>
#include <tvm/runtime/module.h>

#include <string>
#include <utility>
#include <vector>

namespace tvm {
namespace runtime {

// Library module that exposes symbols from a library.
class LibraryModuleNode final : public ModuleNode {
 public:
  explicit LibraryModuleNode(ObjectPtr<Library> lib, FFIFunctionWrapper wrapper)
      : lib_(lib), packed_func_wrapper_(wrapper) {}

  const char* type_key() const final { return "library"; }

  /*! \brief Get the property of the runtime module .*/
  int GetPropertyMask() const final {
    return ModulePropertyMask::kBinarySerializable | ModulePropertyMask::kRunnable;
  };

  ffi::Function GetFunction(const String& name, const ObjectPtr<Object>& sptr_to_self) final {
    TVMFFISafeCallType faddr;
    if (name == runtime::symbol::tvm_module_main) {
      const char* entry_name =
          reinterpret_cast<const char*>(lib_->GetSymbol(runtime::symbol::tvm_module_main));
      ICHECK(entry_name != nullptr)
          << "Symbol " << runtime::symbol::tvm_module_main << " is not presented";
      faddr = reinterpret_cast<TVMFFISafeCallType>(lib_->GetSymbol(entry_name));
    } else {
      faddr = reinterpret_cast<TVMFFISafeCallType>(lib_->GetSymbol(name.c_str()));
    }
    if (faddr == nullptr) return ffi::Function();
    return packed_func_wrapper_(faddr, sptr_to_self);
  }

 private:
  ObjectPtr<Library> lib_;
  FFIFunctionWrapper packed_func_wrapper_;
};

ffi::Function WrapFFIFunction(TVMFFISafeCallType faddr, const ObjectPtr<Object>& sptr_to_self) {
  return ffi::Function::FromPacked([faddr, sptr_to_self](ffi::PackedArgs args, ffi::Any* rv) {
    ICHECK_LT(rv->type_index(), ffi::TypeIndex::kTVMFFIStaticObjectBegin);
    TVM_FFI_CHECK_SAFE_CALL((*faddr)(nullptr, reinterpret_cast<const TVMFFIAny*>(args.data()),
                                     args.size(), reinterpret_cast<TVMFFIAny*>(rv)));
  });
}

void InitContextFunctions(std::function<void*(const char*)> fgetsymbol) {
#define TVM_INIT_CONTEXT_FUNC(FuncName)                                                \
  if (auto* fp = reinterpret_cast<decltype(&FuncName)*>(fgetsymbol("__" #FuncName))) { \
    *fp = FuncName;                                                                    \
  }
  // Initialize the functions
  TVM_INIT_CONTEXT_FUNC(TVMFFIFunctionCall);
  TVM_INIT_CONTEXT_FUNC(TVMFFIErrorSetRaisedFromCStr);
  TVM_INIT_CONTEXT_FUNC(TVMBackendGetFuncFromEnv);
  TVM_INIT_CONTEXT_FUNC(TVMBackendAllocWorkspace);
  TVM_INIT_CONTEXT_FUNC(TVMBackendFreeWorkspace);
  TVM_INIT_CONTEXT_FUNC(TVMBackendParallelLaunch);
  TVM_INIT_CONTEXT_FUNC(TVMBackendParallelBarrier);

#undef TVM_INIT_CONTEXT_FUNC
}

Module LoadModuleFromBinary(const std::string& type_key, dmlc::Stream* stream) {
  std::string loadkey = "runtime.module.loadbinary_";
  std::string fkey = loadkey + type_key;
  const auto f = tvm::ffi::Function::GetGlobal(fkey);
  if (!f.has_value()) {
    LOG(FATAL) << "Binary was created using {" << type_key
               << "} but a loader of that name is not registered."
               << "Perhaps you need to recompile with this runtime enabled.";
  }

  return (*f)(static_cast<void*>(stream)).cast<Module>();
}

/*!
 * \brief Load and append module blob to module list
 * \param mblob The module blob.
 * \param lib The library.
 * \param root_module the output root module
 * \param dso_ctx_addr the output dso module
 */
void ProcessLibraryBin(const char* mblob, ObjectPtr<Library> lib,
                       FFIFunctionWrapper packed_func_wrapper, runtime::Module* root_module,
                       runtime::ModuleNode** dso_ctx_addr = nullptr) {
  ICHECK(mblob != nullptr);
  uint64_t nbytes = 0;
  for (size_t i = 0; i < sizeof(nbytes); ++i) {
    uint64_t c = mblob[i];
    nbytes |= (c & 0xffUL) << (i * 8);
  }
  dmlc::MemoryFixedSizeStream fs(const_cast<char*>(mblob + sizeof(nbytes)),
                                 static_cast<size_t>(nbytes));
  dmlc::Stream* stream = &fs;
  uint64_t size;
  ICHECK(stream->Read(&size));
  std::vector<Module> modules;
  std::vector<uint64_t> import_tree_row_ptr;
  std::vector<uint64_t> import_tree_child_indices;
  int num_dso_module = 0;

  for (uint64_t i = 0; i < size; ++i) {
    std::string tkey;
    ICHECK(stream->Read(&tkey));
    // "_lib" serves as a placeholder in the module import tree to indicate where
    // to place the DSOModule
    if (tkey == "_lib") {
      auto dso_module = Module(make_object<LibraryModuleNode>(lib, packed_func_wrapper));
      *dso_ctx_addr = dso_module.operator->();
      ++num_dso_module;
      modules.emplace_back(dso_module);
      ICHECK_EQ(num_dso_module, 1U) << "Multiple dso module detected, please upgrade tvm "
                                    << " to the latest before exporting the module";
    } else if (tkey == "_import_tree") {
      ICHECK(stream->Read(&import_tree_row_ptr));
      ICHECK(stream->Read(&import_tree_child_indices));
    } else {
      auto m = LoadModuleFromBinary(tkey, stream);
      modules.emplace_back(m);
    }
  }

  // if we are using old dll, we don't have import tree
  // so that we can't reconstruct module relationship using import tree
  if (import_tree_row_ptr.empty()) {
    auto n = make_object<LibraryModuleNode>(lib, packed_func_wrapper);
    auto module_import_addr = ModuleInternal::GetImportsAddr(n.operator->());
    for (const auto& m : modules) {
      module_import_addr->emplace_back(m);
    }
    *dso_ctx_addr = n.get();
    *root_module = Module(n);
  } else {
    for (size_t i = 0; i < modules.size(); ++i) {
      for (size_t j = import_tree_row_ptr[i]; j < import_tree_row_ptr[i + 1]; ++j) {
        auto module_import_addr = ModuleInternal::GetImportsAddr(modules[i].operator->());
        auto child_index = import_tree_child_indices[j];
        ICHECK(child_index < modules.size());
        module_import_addr->emplace_back(modules[child_index]);
      }
    }

    ICHECK(!modules.empty()) << "modules cannot be empty when import tree is present";
    // invariance: root module is always at location 0.
    // The module order is collected via DFS
    *root_module = modules[0];
  }
}

Module CreateModuleFromLibrary(ObjectPtr<Library> lib, FFIFunctionWrapper packed_func_wrapper) {
  InitContextFunctions([lib](const char* fname) { return lib->GetSymbol(fname); });
  auto n = make_object<LibraryModuleNode>(lib, packed_func_wrapper);
  // Load the imported modules
  const char* library_bin =
      reinterpret_cast<const char*>(lib->GetSymbol(runtime::symbol::tvm_ffi_library_bin));

  Module root_mod;
  runtime::ModuleNode* dso_ctx_addr = nullptr;
  if (library_bin != nullptr) {
    ProcessLibraryBin(library_bin, lib, packed_func_wrapper, &root_mod, &dso_ctx_addr);
  } else {
    // Only have one single DSO Module
    root_mod = Module(n);
    dso_ctx_addr = root_mod.operator->();
  }

  // allow lookup of symbol from root (so all symbols are visible).
  if (auto* ctx_addr =
          reinterpret_cast<void**>(lib->GetSymbol(runtime::symbol::tvm_ffi_library_ctx))) {
    *ctx_addr = dso_ctx_addr;
  }

  return root_mod;
}
}  // namespace runtime
}  // namespace tvm
