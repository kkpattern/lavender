"""Generates Visual Studio project files."""

from __future__ import division, print_function, unicode_literals
from collections import namedtuple, OrderedDict
import argparse
import errno
import json
import locale
import os
import re
import subprocess
import sys
import typing

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BAZEL = 'bazel'

PROJECT_TYPE_GUID = '{8BC9CEB8-8B4A-11D0-8D11-00A0C91BC942}'
FOLDER_TYPE_GUID = '{2150E333-8FDC-42A3-9474-1A3956D46DE8}'


class Label:
    PATTERN = re.compile(r'((@[a-zA-Z0-9/._-]+)?//)?([a-zA-Z0-9/._-]*)(:([a-zA-Z0-9_/.+=,@~-]+))?$')

    def __init__(self, name):
        match = re.match(Label.PATTERN, name)
        if not match:
            raise ValueError("Invalid label: " + name)
        self.repo = match.group(2) or None
        self._absolute = True if match.group(1) else False
        if not self._absolute:
            raise NotImplementedError("Absolute package path required")
        self.package = match.group(3)
        self.name = match.group(5) or self.package.split('/')[-1]
        self.unique_name = self.package.replace("/", "#") + "#" + self.name

    @property
    def absolute(self):
        return (self.repo or '') + '//' + self.package + ':' + self.name

    @property
    def package_path(self):
        assert self._absolute
        return os.path.normpath(self.package)

    @property
    def info_path(self):
        """Path to the msbuild info file for this label, relative to the bin path."""
        if self.repo:
            raise NotImplementedError("External repos")
        # TODO: absolute
        return os.path.join(self.package, self.name+'.msbuild')

class Struct:
    pass

class ProjectInfo:
    def __init__(self, label: Label, info_dict):
        self.label = label
        #self.ws_path = info_dict['workspace_root']
        self.rule = Struct()
        self.rule.kind = info_dict['kind']
        self.rule.srcs = info_dict['files']['srcs']
        self.rule.hdrs = info_dict['files']['hdrs']
        self.output_files = info_dict['target']['files']
        self.guid = _generate_uuid_from_data(str(label))

        if self.output_files:
            output_file = self.output_files[0]
            self.output_file = Struct()
            self.output_file.path = os.path.dirname(output_file)
            self.output_file.basename = os.path.basename(output_file)
        else:
            self.output_file = None

        self._cc = info_dict.get('cc', None)

    @property
    def compile_flags_joined(self):
        return ' '.join(self._cc['compile_flags']) if self._cc else ''

    @property
    def defines_joined(self):
        return ';'.join(self._cc['defines']) if self._cc else ''

    def include_dirs_joined(self, cfg, rel_paths):
        cc = self._cc
        if not cc:
            return ''
        paths = cc['include_dirs'] + cc['system_include_dirs'] + cc['quote_include_dirs']
        paths = (self._rewrite_include_path(cfg, rel_paths, path) for path in paths)
        return ';'.join(paths)

    def _rewrite_include_path(self, cfg, rel_paths, path):
        path = path.replace('/', '\\').split('\\')  # MSYS2 confuses Python
        if path[0] == "external":
            path.insert(0, cfg.bazel_root)
        path = [node if node != cfg.default_cfg_dirname else '%(BazelCfgDirname)' for node in path]
        return os.path.normpath(os.path.join(rel_paths.workspace_root, *path))


class FolderInfo(object):
    def __init__(self, folder_path: str, parent_path: str):
        self.folder_path = folder_path
        self.folder_name = self.folder_path.rsplit("/", 1)[-1]
        self.folder_guid = _generate_uuid_from_data("folder:"+self.folder_name)
        self.folder_parent = parent_path


BuildConfig = namedtuple('BuildConfig', ['msbuild_name', 'bazel_name', 'bazel_config'])
PlatformConfig = namedtuple('PlatformConfig', ['msbuild_name', 'bazel_name'])

class Configuration:
    def __init__(self, args):
        self.workspace_root = os.path.abspath('.')  # TODO
        self.output_path    = os.path.abspath(args.output)

        self.paths = Struct()
        self.paths.workspace_root = self.workspace_root
        self.paths.bin = os.path.join(self.workspace_root, 'bazel-bin')
        self.paths.out = os.path.join(self.workspace_root, 'bazel-out')

        self.bazel_root = "bazel-"+os.path.basename(os.getcwd()).lower()

        self._setup_env()
        self._build_target_list(args)

        self.solution_name = args.solution or os.path.basename(os.getcwd())

        self.build_configs = [
            BuildConfig('Debug', 'dbg', ['-c', 'dbg']),
            BuildConfig('RelWithDebInfo', 'opt', ['-c', 'opt', '--copt=/Z7']),
            BuildConfig('Release', 'opt', ['-c', 'opt']),
            BuildConfig('Debug[Remote]', 'dbg', ['-c', 'dbg', '--config=remote']),
            BuildConfig('RelWithDebInfo[Remote]', 'opt', ['-c', 'opt', '--copt=/Z7', '--config=remote']),
            BuildConfig('Release[Remote]', 'opt', ['-c', 'opt', '--config=remote']),
        ]
        self.platforms = [
            PlatformConfig('x64', 'x64_windows')
        ]
        self.user_config_names = args.config or []

        self.system_paths = os.environ['PATH'].split(os.pathsep)
        self._cygpath = self._find_exe('cygpath.exe')

        bazel_path = self._find_exe('bazel.exe') or self._find_exe('bazel')
        if not bazel_path:
            raise StandardError("Could not find bazel or bazel.exe in path")
        self.bazel_path = self.canonical_path(bazel_path)

        self.default_cfg_dirname = 'x64_windows-fastbuild'

        self.cc_workspace_path = self._get_bazel_info()['execution_root']

    def _build_target_list(self, args):
        # If no query, use all targets in the workspace.
        queries = args.query or ['//...']

        kinds = set(['cc_library', 'cc_inc_library', 'cc_binary', 'cc_test'])

        # Use OrderedDict to eliminate duplicates, but keep ordering
        targets = OrderedDict()
        for query in queries:
            for target in self._get_targets_from_query(query, kinds):
                targets[target] = True
                deps = subprocess.check_output([
                    BAZEL,
                    'cquery',
                    '--config=win',
                    "filter('^//', kind(cc_library, deps(//engine/client)))"]).decode()
                for each in deps.split("\n"):
                    if each:
                        t = each.split()[0]
                        targets[t] = True
        self.targets = targets.keys()

    _LABEL_KIND_PATTERN = re.compile(r'(\w+) rule (.+)$')
    def _get_targets_from_query(self, query, kinds):
        labels = []
        target_list = subprocess.check_output([BAZEL, 'query', query, '--output=label_kind'])
        for line in target_list.split(b'\n'):
            line = line.decode('utf-8').strip()
            if not line:
                continue
            match = re.match(Configuration._LABEL_KIND_PATTERN, line.strip())
            if not match:
                raise ValueError("Invalid bazel query output: " + line.strip())
            kind = match.group(1)
            label = match.group(2)
            if kind in kinds:
                labels.append(label)
        return labels

    def _get_bazel_info(self):
        info = subprocess.check_output([BAZEL, 'info']).decode()
        keyvals = (line.strip().split(': ', 1) for line in info.split('\n'))
        return {kv[0]:kv[1] for kv in keyvals if len(kv) == 2}

    @property
    def bin_path(self):
        """Path to the bazel-bin directory of the current workspace."""
        return os.path.join(self.workspace_root, 'bazel-bin')

    def output_path_for_package(self, package):
        """Path to the output directory for files generated for the given package."""
        return os.path.join(self.output_path, package)

    def _setup_env(self):
        """Modifies the env vars of the process for bazel to run successfully."""
        # Tell MSYS2 not to rewrite absolute package paths in command line args.
        # Don't override a more aggressive setting.
        if os.environ.get('MSYS2_ARG_CONV_EXCL') != '*':
            os.environ['MSYS2_ARG_CONV_EXCL'] = '//'

    def _find_exe(self, name):
        for path in self.system_paths:
            program = os.path.join(path, name)
            if os.path.isfile(program) and os.access(program, os.X_OK):
                return program
        return None

    class RelativePathHelper:
        """Provides a set of paths relative to some starting path."""
        def __init__(self, orig_paths, relative_to):
            self.orig_paths = orig_paths
            self.relative_to = relative_to

        def __getattr__(self, name):
            orig = getattr(self.orig_paths, name)
            relpath = os.path.relpath(orig, self.relative_to)
            return os.path.normpath(relpath)

    def rel_paths(self, relative_to):
        return Configuration.RelativePathHelper(self.paths, relative_to)

    def canonical_path(self, path):
        """Returns the OS canonical path (i.e. Windows-style path if in Cygwin)."""
        if self._cygpath:
            out = subprocess.check_output([self._cygpath, '-w', path]).strip()
            if isinstance(out, bytes):
                out = out.decode(locale.getpreferredencoding()).strip()
            return out

        return os.path.normpath(path)

def run_aspect(cfg):
    """Invokes bazel on our aspect to generate target info."""
    subprocess.check_call([
        BAZEL,
        'build',
        '--override_repository=bazel-msbuild={}'.format(os.path.join(SCRIPT_DIR, 'bazel')),
        '--aspects=@bazel-msbuild//bazel-msbuild:msbuild.bzl%msbuild_aspect',
        '--output_groups=msbuild_outputs'] + list(cfg.targets))

def read_info(cfg, target):
    """Reads the generated msbuild info file for the given target."""
    info_dict = json.load(open(os.path.join(cfg.bin_path, target.info_path)))
    return ProjectInfo(target, info_dict)

def _msb_nmake_output(target, rel_paths):
    if not target.output_file:
        return ''
    return ((
            r'<NMakeOutput>{rel_paths.bin}\{target.label.package_path}' +
            r'\{target.output_file.basename}</NMakeOutput>'
        ).format(target=target, rel_paths=rel_paths)
    )

def _msb_target_name_ext(target):
    if not target.output_file:
        name = target.label.unique_name
        ext = ""
    else:
        if '.' in target.output_file.basename:
            name, ext = target.output_file.basename.rsplit('.', 1)
        else:
            name, ext = target.output_file.basename, ''
    return r'<TargetName>{}</TargetName><TargetExt>{}</TargetExt>'.format(name, ext)

def _add_filter_to_set(filters, filter_name):
    """Adds a filter, and all its parent filters to the set `filters`."""
    DELIM = '\\'
    if filter_name in filters:
        return
    components = filter_name.split(DELIM)
    path = components[0]
    filters.add(path)
    for component in components[1:]:
        path += DELIM + component
        filters.add(path)

def _msb_file_filter(info, filename, filters):
    # In most cases, files in a package are in the same directory as that package.
    # If they are in another directory, we add a filter to the file to help
    # keep things organized the same way they are in the codebase.

    # During generation of main project file, we don't include filters info.
    if filters is None:
        return ''

    # During generation of filters file, we return None to indicate not to
    # generate any contet for this file.
    # TODO: This is really hacky!
    dirname = os.path.dirname(filename)
    if not dirname:
        return None
    filter_name = os.path.relpath(dirname, info.label.package_path).replace('/', '\\')
    if not filter_name or filter_name == '.':
        return None
    _add_filter_to_set(filters, filter_name)
    return '<Filter>{}</Filter>'.format(filter_name)

def _msb_cc_src(rel_ws_root, info, filters, filename):
    filter = _msb_file_filter(info, filename, filters)
    if filter is None:
        return None
    return '<ClCompile Include="{name}">{filter}</ClCompile>'.format(
        name=os.path.join(rel_ws_root, filename),
        filter=filter)

def _msb_cc_inc(rel_ws_root, info, filters, filename):
    filter = _msb_file_filter(info, filename, filters)
    if filter is None:
        return None
    return '<ClInclude Include="{name}">{filter}</ClInclude>'.format(
        name=os.path.join(rel_ws_root, filename),
        filter=filter)

def _msb_item_group(rel_ws_root, info, filters, file_targets, func):
    if not file_targets:
        return ''
    xml_items = [func(rel_ws_root, info, filters, f) for f in file_targets]
    return (
        '\n  <ItemGroup>' +
        '\n    '.join([''] + [item for item in xml_items if item is not None]) +
        '\n  </ItemGroup>'
    )

def _msb_files(cfg, info, filters=None):
    """Set filters to a set-like when writing filters. All filters used will be added to the set."""
    output_dir = cfg.output_path_for_package(info.label.package)
    rel_ws_root = cfg.cc_workspace_path  #os.path.relpath(cfg.workspace_root, output_dir)
    return (
        _msb_item_group(rel_ws_root, info, filters, info.rule.srcs, _msb_cc_src) +
        _msb_item_group(rel_ws_root, info, filters, info.rule.hdrs, _msb_cc_inc))


def _sln_project(project: ProjectInfo) -> str:
    # This first UUID appears to be an identifier for Visual C++ packages?
    return (
        'Project("{type_guid}") = "{name}", "{package}\\{name}.vcxproj", "{guid}"\nEndProject'
        .format(guid=project.guid, type_guid=PROJECT_TYPE_GUID,
                name=project.label.unique_name, package=project.label.package))


def _sln_folder(folder_info: FolderInfo) -> str:
    return (
        'Project("{type_guid}") = "{name}", "{name}", "{guid}"\nEndProject'
        .format(type_guid=FOLDER_TYPE_GUID,
                name=folder_info.folder_name,
                guid=folder_info.folder_guid))


def _sln_projects(folders: typing.Dict[str, FolderInfo],
                  projects: typing.List[ProjectInfo]) -> str:
    result = [_sln_folder(folders[k]) for k in folders]
    result.extend([_sln_project(project) for project in projects])
    return '\n'.join(result)


def _sln_cfgs(cfg):
    lines = []
    for build_config in cfg.build_configs:
        for platform in cfg.platforms:
            lines.append(
                '{cfg}|{platform} = {cfg}|{platform}'
                .format(cfg=build_config.msbuild_name, platform=platform.msbuild_name))
    return '\n\t\t'.join(lines)

def _sln_project_cfgs(cfg, projects):
    lines = []
    for build_config in cfg.build_configs:
        for platform in cfg.platforms:
            for project in projects:
                fmt = {
                    'guid': project.guid,
                    'cfg':  build_config.msbuild_name,
                    'platform': platform.msbuild_name
                }
                lines.extend([
                    '{guid}.{cfg}|{platform}.ActiveCfg = {cfg}|{platform}'.format(**fmt),
                    '{guid}.{cfg}|{platform}.Build.0 = {cfg}|{platform}'.format(**fmt),
                ])
    return '\n\t\t'.join(lines)


def _sln_project_to_parent(project_guid, parent_guid) -> str:
    return f"{project_guid} = {parent_guid}"


def _sln_nested_projects(folders: typing.Dict[str, FolderInfo],
                         projects: typing.List[ProjectInfo]) -> str:
    result = []
    for k in folders:
        each = folders[k]
        if each.folder_parent:
            line = _sln_project_to_parent(
                each.folder_guid,
                folders[each.folder_parent].folder_guid)
            result.append(line)
    for each in projects:
        if each.label.package:
            line = _sln_project_to_parent(
                each.guid,
                folders[each.label.package].folder_guid)
            result.append(line)
    return "\n\t\t".join(result)


def _msb_project_cfgs(cfg):
    configs = []
    for build_config in cfg.build_configs:
        for platform in cfg.platforms:
            configs.append(r'''
    <ProjectConfiguration Include="{cfg}|{platform}">
      <Configuration>{cfg}</Configuration>
      <Platform>{platform}</Platform>
    </ProjectConfiguration>'''
                .format(cfg=build_config.msbuild_name, platform=platform.msbuild_name)
            )
    return ''.join(configs)

def _msb_cfg_properties(cfg: Configuration):
    props = []
    user_config = ''.join(' --config=' + name for name in cfg.user_config_names)
    for build_config in cfg.build_configs:
        for platform in cfg.platforms:
            builtin_config = list(build_config.bazel_config)
            props.append(r'''
  <PropertyGroup Condition="'$(Configuration)|$(Platform)'=='{cfg.msbuild_name}|{platform.msbuild_name}'">
    <BazelCfgOpts>{builtin_config}{user_config}</BazelCfgOpts>
    <BazelCfgDirname>{platform.bazel_name}-{cfg.bazel_name}</BazelCfgDirname>
  </PropertyGroup>'''.format(cfg=build_config,
                             platform=platform,
                             builtin_config=" ".join(builtin_config),
                             user_config=user_config))
    return '\n'.join(props)

def _msb_filter_items(filters):
    tags = [r'''
    <Filter Include="{name}">
      <UniqueIdentifier>{uuid}</UniqueIdentifier>
    </Filter>'''.format(name=name, uuid=_generate_uuid_from_data(name)) for name in filters]
    return '\n'.join(tags)

def _generate_uuid_from_data(data):
    # We don't comply with any UUID standard, but we use 3 to advertise that it is a deterministic
    # hash of a name. I don't think Visual Studio will complain about the method used to create our
    # one-way hash.
    # TODO: Actually use more bits.
    hsh = abs(hash(data))
    part1 = hsh // (2**32)
    part2 = hsh % (2**32)
    return '{{{:08X}-0000-3000-A000-0000{:08X}}}'.format(part1, part2)

def _makedirs(path):
    """Ensures that the directories in path exist. Does nothing if they do."""
    try:
        os.makedirs(path)
    except OSError as exc:
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise

def _generate_project_filters(filters_template, cfg, info):
    filters = set()
    file_groups = _msb_files(cfg, info, filters)

    return filters_template.format(
        file_groups=file_groups,
        filter_items=_msb_filter_items(filters))

def generate_projects(cfg):
    with open(os.path.join(SCRIPT_DIR, 'templates', 'vcxproj.xml')) as f:
        template = f.read()
    with open(os.path.join(SCRIPT_DIR, 'templates', 'vcxproj.filters.xml')) as f:
        filters_template = f.read()
    project_configs = _msb_project_cfgs(cfg)
    config_properties = _msb_cfg_properties(cfg)

    project_infos = []
    for target in cfg.targets:
        info = read_info(cfg, Label(target))
        project_infos.append(info)

        project_dir = os.path.join(cfg.output_path, info.label.package)
        rel_paths = cfg.rel_paths(project_dir)
        content = template.format(
            cfg=cfg,
            target=info,
            target_name_ext=_msb_target_name_ext(info),
            project_configs=project_configs,
            config_properties=config_properties,
            outputs=';'.join([os.path.basename(f) for f in info.output_files]),
            file_groups=_msb_files(cfg, info),
            rel_paths=rel_paths,
            nmake_output=_msb_nmake_output(info, rel_paths),
            include_dirs_joined=info.include_dirs_joined(cfg, rel_paths))
        filters_content = _generate_project_filters(filters_template, cfg, info)

        _makedirs(project_dir)
        with open(os.path.join(project_dir, info.label.unique_name+'.vcxproj'), 'w') as out:
            out.write(content)
        with open(os.path.join(project_dir, info.label.unique_name+'.vcxproj.filters'), 'w') as out:
            out.write(filters_content)

    return project_infos


def _generate_folders(
    project_infos: typing.List[ProjectInfo]
) -> typing.Dict[str, FolderInfo]:
    folder = OrderedDict()
    for project in project_infos:
        package = project.label.package
        while package:
            if package not in folder:
                s = package.rsplit("/", 1)
                if len(s) == 2:
                    parent = s[0]
                else:
                    parent = None
                folder[package] = FolderInfo(package, parent)
                package = parent
            else:
                break
    # Keep upper folder in the front.
    reversed_result = OrderedDict()
    for k in reversed(folder):
        reversed_result[k] = folder[k]
    return reversed_result


def generate_solution(cfg, project_infos: typing.List[ProjectInfo]):
    with open(os.path.join(SCRIPT_DIR, 'templates', 'solution.sln')) as f:
        template = f.read()
    sln_filename = os.path.join(cfg.output_path, cfg.solution_name+'.sln')
    folders = _generate_folders(project_infos)
    content = template.format(
        projects=_sln_projects(folders, project_infos),
        cfgs=_sln_cfgs(cfg),
        project_cfgs=_sln_project_cfgs(cfg, project_infos),
        nested_projects=_sln_nested_projects(folders, project_infos),
        guid=_generate_uuid_from_data(sln_filename))
    with open(sln_filename, 'w') as out:
        out.write(content)

def main(argv):
    parser = argparse.ArgumentParser(
        description="Generates Visual Studio project files from Bazel projects.")
    parser.add_argument("query", nargs='*',
                        help="Target query to generate project for [default: all targets]")
    parser.add_argument("--output", "-o", type=str, default='msbuild',
                        help="Output directory")
    parser.add_argument("--solution", "-n", type=str,
                        help="Solution name [default: current directory name]")
    parser.add_argument("--config", action='append',
                        help="Additional --config option to pass to bazel; may be used multiple times")
    args = parser.parse_args(argv[1:])

    cfg = Configuration(args)

    run_aspect(cfg)
    project_infos = generate_projects(cfg)
    generate_solution(cfg, project_infos)

if __name__ == '__main__':
    main(sys.argv)
