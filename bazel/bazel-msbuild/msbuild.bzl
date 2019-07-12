def _get_project_info(target, ctx):
  cc = getattr(target, 'cc', None)
  if cc:
    cc_info = struct(
      include_dirs        = cc.include_directories,
      system_include_dirs = cc.system_include_directories,
      quote_include_dirs  = cc.quote_include_directories,
      compile_flags       = cc.compile_flags + ctx.fragments.cpp.compiler_options([]) + ctx.fragments.cpp.cxx_options([]),
      defines             = cc.defines,
    )
  else:
    cc_info = None
  return struct(
      workspace_root = ctx.label.workspace_root,
      package        = ctx.label.package,

      files = struct(**{name: _get_file_group(ctx.rule.attr, name) for name in ['srcs', 'hdrs']}),
      deps  = [str(dep.label) for dep in getattr(ctx.rule.attr, 'deps', [])],
      target = struct(label=str(target.label), files=[f.path for f in target.files.to_list()]),
      kind = ctx.rule.kind,

      cc = cc_info,
  )

def _get_file_group(rule_attrs, attr_name):
  file_targets = getattr(rule_attrs, attr_name, None)
  if not file_targets: return []
  return [file.path for t in file_targets for file in t.files.to_list()]

def _msbuild_aspect_impl(target, ctx):
  info_file = ctx.actions.declare_file(target.label.name + '.msbuild')
  content = _get_project_info(target, ctx).to_json()
  ctx.actions.write(info_file, content, is_executable=False)

  dep_outputs = []
  for dep in getattr(ctx.rule.attr, 'deps', []):
    dep_outputs.append(dep[OutputGroupInfo].msbuild_outputs)
  outputs = depset([info_file], transitive = dep_outputs)
  return [OutputGroupInfo(msbuild_outputs=outputs)]

msbuild_aspect = aspect(
    attr_aspects = ["deps"],
    fragments    = ["cpp"],
    implementation = _msbuild_aspect_impl,
)
