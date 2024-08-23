from bob.generators.common import CommonIDEGenerator, INVALID_CHAR_TRANS, filterDirs, BaseScanner
from bob.generators.VisualStudioCode import JSON_WORKSPACE_TEMPLATE, getCorrectPath, getCorrectPathList
from bob.errors import BuildError, ParseError
from bob.stringparser import Env

import subprocess
from subprocess import Popen, PIPE
import tempfile

import argparse
import json
import re
import stat
import os

from pathlib import Path

# environment variables we don't need to export
ENV_BLACKLIST = [
    "AR", "CC", "CXX", "LD", "OBJCOPY", "STRIP",
    "LD_LIBRARY_PATH",
    "RANLIB",
    "PYTHON3_VERSION", "CYTHONPATH3",
    "PYTHON_VERSION", "CYTHONPATH",
    "PYTHONPATH",
    "PATH",
    "MEMCACHE_SERVER",
    "GNAT_TOOLCHAIN_HOST", "PV",
    "PARALLEL_SPARKSIMP",
    "AUTOCONF_HOST", "AUTOCONF_BUILD", "CROSS_COMPILE",
    "LDFLAGS", "CPPFLAGS", "CFLAGS", "CXXFLAGS",
    "MAKE_JOBS",
    "__SRC_DIR", "PWD", "BOB_CWD", "OLDPWD"
    ];

SOURCE_FILES = re.compile(r"\.[c](pp)?$|\.adb$", re.IGNORECASE)
HEADER_FILES = re.compile(r"\.[h](pp)?$|\.ads$", re.IGNORECASE)
RESOURCE_FILES = re.compile(r"\.cmake$|^CMakeLists.txt$", re.IGNORECASE)
GPR_FILES = re.compile(r"\.gpr$", re.IGNORECASE)

class CheckoutInfo:
    def __init__(self, scan, packageVid):
        self.scan = scan
        self.packages = set([packageVid])
        self.name = None
        self.isUsedDist = False

class PackageInfo:
    def __init__(self, recipeName, packageName, isRoot):
        self.isRoot = isRoot
        self.recipeName = recipeName
        self.packageName = packageName
        self.checkout = False
        self.dependencies = set()

class AdaScanner(BaseScanner):
    def __init__(self, isRoot, stack, additionalFiles, ignore):
        super().__init__(isRoot, stack, additionalFiles)
        self.__sources = set()
        self.__headers = set()
        self.__resources = set()
        self.__gprFiles = set()
        self.__ignore = ignore
        self.isRoot = isRoot

    def __addFile (self, root, fileName, debug = False):
        added = True
        if debug: print(" ADD: " + fileName, end="")
        if SOURCE_FILES.search (fileName):
            self.__sources.add(os.path.join(root,fileName))
            if debug: print("  -> Source")
        elif HEADER_FILES.search(fileName):
            self.__headers.add(os.path.join(root,fileName))
            if debug: print("  -> Header")
        elif GPR_FILES.search(fileName):
            self.__gprFiles.add(os.path.join(root,fileName))
            if debug: print("  -> GPR")
        elif RESOURCE_FILES.search(fileName):
            self.__resources.add(os.path.join(root,fileName))
            if debug: print("  -> RES")
        else:
            if debug: print("  -> Unknown")
            added = False
        return added

    def filterIgnoreDirs(self, directories):
        i = 0
        while i < len(directories):
            if directories[i] in self.__ignore:
                # print("Ignore Dir: " + directories[i])
                del directories[i]
            else:
                i += 1


    def scan(self, workspacePath, debug = False):
        super().scan(workspacePath)
        ret = False
        if debug: print("SCANNING: " + workspacePath)
        for ignore in self.__ignore:
            if ignore in workspacePath:
                # print("Ignore WS: " + workspacePath)
                return ret
        for root, directories, filenames in os.walk(workspacePath):
            # remove scm directories, os.walk will only descend into directories
            # that stay in the list
            filterDirs(directories)
            self.filterIgnoreDirs(directories)
            if debug:
                print("Processing dirs: " + str(directories))
            for filename in filenames:
                ret = self.__addFile (root, filename, debug) or ret
        if debug: print("RET: " + str(ret))
        return ret

    def headers(self):
        return sorted(self.__headers)

    def sources(self):
        return sorted(self.__sources)

    def resources(self):
        return sorted(self.__resources)

    def gprFiles(self):
        return sorted(self.__gprFiles)

class Project:
    def __init__(self, recipesRoot, scan):
        self.isRoot = scan.isRoot
        self.packagePath = scan.stack
        self.workspacePath =  getCorrectPath(recipesRoot, scan.workspacePath)
        self.headers = getCorrectPathList(recipesRoot, scan.headers())

        self.sources = getCorrectPathList(recipesRoot, scan.sources())
        self.resources = getCorrectPathList(recipesRoot, scan.resources())
        self.gprFiles = getCorrectPathList(recipesRoot, scan.gprFiles())
        self.incPaths =  getCorrectPathList(recipesRoot, scan.incPaths)
        self.dependencies = scan.dependencies
        self.runTargets = getCorrectPathList(recipesRoot, scan.runTargets)

class GeneratorAda(CommonIDEGenerator):
    def __init__(self, name):
        parser = argparse.ArgumentParser(prog="bob project "+name,
                                         description="Generate workspace for Ada projects")

        parser.add_argument('--name', metavar="NAME", required=True,
            help="Name of project.")
        parser.add_argument('--destination', metavar="DEST",
            help="Destination of project files (default: projects/NAME).")
        parser.add_argument('-D', dest="defines", action='append',
                            help="Add custom defines. Key=Value.", default=[])

        parser.add_argument("-S", '--use-src', action='append', default=[],
                            help="Use src workspace for package.")
        parser.add_argument("-K", '--keep', action='append', default=[],
                            help="Use dist  workspace for package.")

        parser.add_argument('-i','--ignore', action='append',
                            help="Ignore source dirs.", default=[])
        parser.add_argument('--sort', action='store_true',
                            help="Sort the dependencies by name (default: unsorted)")
        parser.add_argument('--gpr', help="GPR File for single project use.", default=[], action='append')

        self.parser = parser
        self.excludes = []

        self.env = {}
        self.path = []
        self.rts = ""
        self.removeWorkspaces = []

        self.gprFiles = {}
        self.checkouts = {}
        self.dists = {}
        self.__packages = {}

        # we need to have a valid gnatToolchain and ada_language_server in the path
        self.gnatToolchain = None
        self.ada_language_server = None
        self.gprbuild = None

    # get the env used for building a package
    def __getBuildEnv(self, package):
        env = {}
        ALL_PATHS={}
        for d in package.getBuildStep().getAllDepSteps():
            if d.isPackageStep():
                ALL_PATHS[d.getPackage().getName()] = d.getWorkspacePath()
        DEP_PATHS={}
        for d in package.getDirectDepSteps():
            if d.isPackageStep():
                DEP_PATHS[d.getPackage().getName()] = d.getWorkspacePath()
        TOOL_PATHS={}
        for n,t in package.getBuildStep().getTools().items():
            TOOL_PATHS[n] = os.path.join(t.getStep().getWorkspacePath(),t.getPath())

        stepEnv = package.getBuildStep().getEnv()
        stepEnv['BOB_CWD'] = os.path.abspath(package.getBuildStep().getWorkspacePath())

        # run the setup script and grep the env
        try:
            tmp = tempfile.NamedTemporaryFile(mode="w", delete=False)

            st = os.stat(tmp.name)
            os.chmod(tmp.name, st.st_mode | stat.S_IEXEC)
            tmp.write("#!/bin/bash\n\n")
            tmp.write("cd ${BOB_CWD}\n")
            tmp.write("set -o errtrace -o nounset -o pipefail\n")
            tmp.write("declare -A _BOB_SOURCES=( [0]=\"Bob prolog\" )\n")
            tmp.write("declare -A BOB_ALL_PATHS=( {} )\n".format(" ".join(sorted(
             [ "[{}]={}".format(name, os.path.abspath(path))
                 for name,path in ALL_PATHS.items() ] ))))
            tmp.write("declare -A BOB_DEP_PATHS=( {} )\n".format(" ".join(sorted(
             [ "[{}]={}".format(name, os.path.abspath(path))
                 for name,path in ALL_PATHS.items() ] ))))

            tmp.write("declare -A BOB_TOOL_PATHS=( {} )\n".format(" ".join(sorted(
             [ "[{}]={}".format(name, os.path.abspath(path))
                 for name,path in TOOL_PATHS.items() ] ))))

            tmp.write(package.getBuildStep().getSetupScript())
            tmp.write("\nenv")
            tmp.flush()
            tmp.close()

            command = [tmp.name, os.path.abspath(package.getCheckoutStep().getWorkspacePath())] # add $1
            command.extend([os.path.abspath(p) for n,p in ALL_PATHS.items()]) # add ${2:}

            setupScript = subprocess.Popen(command, stdout=PIPE, stderr=PIPE, env=stepEnv)
            stdout, stderr = setupScript.communicate()
            # print(stdout.decode("utf-8"))
            for l in stdout.decode("utf-8").splitlines():
                k,v = l.split("=", 1)
                env[k] = v
        finally:
            os.remove(tmp.name)
        #print("===================")
        #print(package.getName())
        #print(env)
        #print("===================")
        return env

    # collect all ada packages here. we start searching the source folders as we usually want to
    # edit our sources. As some packages were generated we need to scan dist as well
    def __collect(self, package, rootPackage):

        if rootPackage:
            rts = package.getBuildStep().getEnv()["RTS"]
            if self.rts == "":
                self.rts = rts
            else:
                if rts != self.rts:
                    raise BuildError("specified multiple roots with different RTS settings.")

            if self.gnatToolchain is None:
                if 'gnat-toolchain' in package.getPackageStep().getTools():
                    t = package.getPackageStep().getTools()['gnat-toolchain']
                    self.gnatToolchain = os.path.join(os.getcwd(),
                            t.getStep().getWorkspacePath(),
                            t.getPath())
                else:
                    # fall back to target toolchain
                    t = package.getPackageStep().getTools()['target-toolchain']
                    self.gnatToolchain = os.path.join(os.getcwd(),
                            t.getStep().getWorkspacePath(),
                            t.getPath())
            if self.gprbuild is None and \
               'gprbuild' in package.getPackageStep().getTools():
                t = package.getPackageStep().getTools()['gprbuild']
                self.gprbuild = os.path.join(os.getcwd(),
                        t.getStep().getWorkspacePath(),
                        t.getPath())
            if self.ada_language_server is None and \
                'ada_language_server' in package.getPackageStep().getTools():
                t = package.getPackageStep().getTools()['ada_language_server']
                self.ada_language_server = os.path.join(os.getcwd(),
                        t.getStep().getWorkspacePath(),
                        t.getPath())

        # only once per package
        packageVid = package.getPackageStep().getVariantId()
        if packageVid in self.__packages: return
        # ignore packages with incompatible RTS
        if self.rts != package.getBuildStep().getEnv().get("RTS", self.rts): return
        self.__packages[packageVid] = packageInfo = \
            PackageInfo(package.getRecipe().getName(), package.getName(), rootPackage)

        # only scan if:
        # rootPackage or package matches packages named on commandline and
        # checkout step is valid and was not already scanned
        use_src = False
        for s in self.use_src:
            if re.match(s, package.getName()):
                use_src = True
                break

        keep = False
        for s in self.args.keep:
            if re.match(s, package.getName()):
                keep = True
                break

        checkoutPath = package.getCheckoutStep().isValid() and \
            package.getCheckoutStep().getWorkspacePath()

        if checkoutPath and (use_src or rootPackage) and not keep:
            print("    SRC: {} - {}".format(package.getName(), package.getCheckoutStep().getWorkspacePath()))
            _gpr_path_append = []
            if checkoutPath not in self.checkouts:
                scan = AdaScanner(rootPackage, "/".join(package.getStack()), [], self.args.ignore)
                scan.scan(checkoutPath)
                info = self.checkouts[checkoutPath] = CheckoutInfo(scan, packageVid)
            else:
                info = self.checkouts[checkoutPath]
                info.packages.add(packageVid)

            # for each package where we want to edit the sources grep the build env and update the existing env.
            # Warn on duplicated env vars with different values
            buildEnv = self.__getBuildEnv(package)
            for k,v in buildEnv.items():
                _v = self.env.get(k)
                if _v is not None and _v != v:
                    if k == "GPR_PROJECT_PATH":
                        # only append. the gprProjectPath is cleaned later
                        self.env[k] = _v + ":" + v
                    elif k == "LARGS_AS_STRING":
                        self.env[k] = _v + " " + v
                    elif not k in ENV_BLACKLIST:
                        print("\033[93m WARN: multiple definitions of {}: \033[0m {} vs. {}".format(k, _v, v))
                else:
                    self.env[k] = v

            for gpr in info.scan.gprFiles():
                _gpr_path_append.append(os.path.abspath(os.path.dirname(gpr)))

            _gpr_path_append = set(_gpr_path_append)
            if len(_gpr_path_append):
                #print(" Append: {}".format(_gpr_path_append))
                self.env["GPR_PROJECT_PATH"] += ":" + ":".join(_gpr_path_append)
                if package.getPackageStep().isValid():
                    self.removeWorkspaces.append(package.getPackageStep().getWorkspacePath())
        else:
            print("    DIST: {} - {}".format(package.getName(), package.getPackageStep().getWorkspacePath()))
            # only scan if package step is valid and was not already scanned
            distPath = package.getPackageStep().isValid() and \
                    package.getPackageStep().getWorkspacePath()
            if distPath and distPath not in self.dists:
                scan = AdaScanner(rootPackage, "/".join(package.getStack()), [], [])
                if scan.scan(distPath):
                    # only add if relevant for us (contains gpr file)
                    if len(scan.gprFiles()) > 0:
                        self.dists[distPath] = CheckoutInfo(scan, packageVid)

        # descend on used dependencies
        packageInfo.checkout = checkoutPath
        for d in package.getBuildStep().getArguments():
            depName = d.getPackage().getName()
            if any(e.search(depName) for e in self.excludes):
                continue
            if d.getPackage() == package:
                continue
            packageInfo.dependencies.add(d.getVariantId())
            self.__collect(d.getPackage(), False)


    # The main goal is to pick up add ada deps and construct a valid
    # GPR_PROJECT_PATH. Otherwise the Ada Language Server won't work.
    # To achieve this we recursively loop through the gpr files.
    def configure(self, packages, argv):

        self.args  = self.parser.parse_args(argv)

        self.use_src=[]
        for p in self.args.use_src:
            try:
                self.use_src.append(re.compile(p))
            except re.error as e:
                raise ParseError("Invalid expression: '{}': {}".format(p, e))

        self.projectName = self.args.name.translate(INVALID_CHAR_TRANS)
        self.destination = (self.args.destination or
            os.path.join("projects", self.projectName)).translate(INVALID_CHAR_TRANS)

        for d in self.args.defines:
            if not "=" in d:
                print("Invalid Define: " + d)
            else:
                k,v = d.split("=")
                self.env[k] = v

        packages_found = 0
        for p in packages:
            packages_found += 1
            self.__collect(p, True)

        if packages_found == 0:
            raise BuildError("Your query matched no packages!");

        if self.env.get("GPR_PROJECT_PATH") is None:
            raise BuildError("GPR_PROJECT_PATH not set by the package!")

        # clean up the GPR_PROJECT_PATH variable
        # for now we have all dist, maybe duplicates and a couple of sources.
        # Remove the dist folders from the packages which should be used as source packages as well as all
        # dulicate entries
        gpr_prj_path = list(set(self.env.get("GPR_PROJECT_PATH").split(':')))

        gpr_prj_path_filtered = []
        for p in gpr_prj_path:
            add = True
            for r in self.removeWorkspaces:
                if r in p:
                    add = False
                    break
            if add:
                gpr_prj_path_filtered.append(p)
        del self.env["GPR_PROJECT_PATH"]
        self.gprProjectPath = gpr_prj_path_filtered

        # Loop through all checkouts and assign a name. If there is a single
        # package using it then we take the package name. Otherwise we take the
        # recipe name. If multiple checkouts have the same name we add a
        # counting suffix.
        names = set()
        for checkoutPath, info in sorted(self.checkouts.items()):
            checkoutPackages = [ self.__packages[vid] for vid in sorted(info.packages) ]
            if len(checkoutPackages) > 1:
                # pick the first name if multiple recipes refer to the same checkout
                name = sorted(set(p.recipeName for p in checkoutPackages))[0]
            else:
                name = checkoutPackages[0].packageName

            name = name.translate(INVALID_CHAR_TRANS)
            suffix = ""
            num = 1
            while name+suffix in names:
                suffix = "-{}".format(num)
                num += 1
            name += suffix
            names.add(name)
            info.name = name

        for checkoutPath, info in sorted(self.dists.items()):
            checkoutPackages = [ self.__packages[vid] for vid in sorted(info.packages) ]
            if len(checkoutPackages) > 1:
                # pick the first name if multiple recipes refer to the same checkout
                name = sorted(set(p.recipeName for p in checkoutPackages))[0]
            else:
                name = checkoutPackages[0].packageName

            name = name.translate(INVALID_CHAR_TRANS)
            suffix = ""
            num = 1
            while name+suffix in names:
                suffix = "-{}".format(num)
                num += 1
            name += suffix
            names.add(name)
            info.name = name

        return 0

    def generate(self, extra, bobRoot):
        self.packages = {}
        done = set()

        self.rootPackages = set()

        for vid in self.__packages:
            if vid in done: continue
            info = self.__packages[vid]
            done.add(vid)
            co = self.checkouts.get(info.checkout)
            if co:
                self.packages[co.name] = co.scan
                if info.isRoot:
                    self.rootPackages.add(co.name)

        gprProjectFiles = []
        for root in self.rootPackages:
            scan = self.packages[root]

            # if `--gpr` is used only pick up the matching gprFiles
            if (len(self.args.gpr) > 0):
                #print("Looking for matching gpr files: {} in {}".format(self.args.gpr, scan.gprFiles()))
                gprProjectFiles.extend([g for g in scan.gprFiles() for a in self.args.gpr if re.match(a, os.path.basename(g)) ])
            else:
                # pick up all gpr files in the source folder
                gprProjectFiles.extend([g for g in scan.gprFiles()])

        print("GPR-Files:\n - " + "\n - ".join(gprProjectFiles))

        # add all used packages
        done = set()
        for vid in self.dists:
            if vid in done: continue
            info = self.dists[vid]
            if info.isUsedDist:
               done.add(vid)
               self.packages[info.name] = info.scan

        if not os.path.exists(self.destination):
            os.makedirs(self.destination)

        if self.ada_language_server is None:
            raise BuildError("No Ada Language server tool found!")

        if self.gnatToolchain is None:
            raise BuildError("No gnatToolchain found!")

        if len(gprProjectFiles) == 0:
            raise BuildError("No matching gpr files found!")
        elif len(gprProjectFiles) == 1:
            self.defaultGprPath = os.path.abspath(gprProjectFiles[0])
        else:
            self.defaultGprPath = os.path.join(os.getcwd(), self.destination, self.projectName+"_default.gpr")
            with open(self.defaultGprPath, "w") as default_gpr:
                for gpr in gprProjectFiles:
                     name = os.path.splitext(os.path.basename(gpr))[0]
                     default_gpr.write("with \"" + name +"\";\n")
                default_gpr.write("project " + self.projectName + "_default is\n")
                default_gpr.write(" Null;\n")
                default_gpr.write("end " + self.projectName + "_default;\n")


ADA_SETTINGS_TEMPLATE = {
   "ada.projectFile": "",

   "terminal.integrated.env.linux": {
      "PATH": "${env:PATH}",
      "GPR_PROJECT_PATH": "",
   },
}

class VsCodeGeneratorAls(GeneratorAda):
    def __init__(self):
        super().__init__("als_vscode")

    def configure(self, packages, argv):
        super().configure(packages, argv)

    def generate(self, extra, bobRoot):
        super().generate(extra, bobRoot)

        JSON_WORKSPACE_TEMPLATE["tasks"]["options"]["cwd"] = str(Path(os.getcwd()))
        JSON_WORKSPACE_TEMPLATE["tasks"]["options"]["shell"]["executable"] = "sh"
        JSON_WORKSPACE_TEMPLATE["tasks"]["options"]["shell"]["args"] = "[-c]"

        projects = {
            name : Project(Path(os.getcwd()), scan)
            for name,scan in self.packages.items()
        }

        workspaceProjectList = []
        for name,project in sorted(projects.items()) if self.args.sort else projects.items():
            workspaceProjectList.append({
                "name": name,
                "path": str(project.workspacePath)
            })

        JSON_WORKSPACE_TEMPLATE["folders"] = workspaceProjectList

        ADA_SETTINGS_TEMPLATE["terminal.integrated.env.linux"]["GPR_PROJECT_PATH"] = \
                ":".join(self.gprProjectPath)

        ADA_SETTINGS_TEMPLATE["ada.projectFile"] = self.defaultGprPath
        # add gnatToolchain and ada_language_server to path
        ADA_SETTINGS_TEMPLATE["terminal.integrated.env.linux"]["PATH"] += ":" + \
                self.gnatToolchain
        ADA_SETTINGS_TEMPLATE["terminal.integrated.env.linux"]["PATH"] += ":" + \
                self.ada_language_server

        for k,v in self.env.items():
            if not k in ENV_BLACKLIST:
                ADA_SETTINGS_TEMPLATE["terminal.integrated.env.linux"][k] = v

        JSON_WORKSPACE_TEMPLATE["settings"].update(ADA_SETTINGS_TEMPLATE)

        self.updateFile(os.path.join(self.destination, self.projectName+".code-workspace"),
                json.dumps(JSON_WORKSPACE_TEMPLATE, indent=4),
                encoding="utf-8", newline='\r\n')

def als_vscode(package, argv, extra, bobRoot):
    generator = VsCodeGeneratorAls()
    generator.configure(package, argv)
    generator.generate(extra, bobRoot)


class EmacsGenerator(GeneratorAda):
    def __init__(self):
        super().__init__("als_emacs")

    def configure(self, packages, argv):
        super().configure(packages, argv)

    def generate(self, extra, bobRoot):
        super().generate(extra, bobRoot)

        projects = {
            name : Project(Path(os.getcwd()), scan)
            for name,scan in self.packages.items()
        }

        with open(os.path.join(self.destination, self.projectName+".emacs"), "w") as s:
            s.write("; enable trace messages for lsp\n")
            s.write(";(setq lsp-log-io t)\n")
            s.write("(setq ada-face-backend 'other)\n")
            s.write("(setq ada-indent-backend 'other)\n")
            s.write("(setq ada-xref-backend 'other)\n")
            s.write("(setq ada-auto-case nil)\n")
            s.write("\n")
            s.write("(require 'use-package)\n")
            s.write("(require 'lsp-lens)\n")
            s.write("(require 'lsp-modeline)\n")
            s.write("(require 'lsp-headerline)\n")
            s.write("\n")
            s.write("(use-package ada-mode\n")
            s.write("  :config\n")
            s.write("   (setq lsp-ada-als-executable \"" + os.path.join(self.ada_language_server, "ada_language_server") + "\")\n")
            s.write("   (setq lsp-ada-project-file \"" + self.defaultGprPath + "\")\n")
            s.write(")\n")
            s.write(" \n")
            s.write("(use-package lsp-mode\n")
            s.write("  :init\n")
            s.write("  ;; set prefix for lsp-command-keymap (few alternatives - \"C-l\", \"C-c l\")\n")
            s.write("  ;(setq lsp-keymap-prefix \"C-c l\")\n")
            s.write("  :hook (\n")
            s.write("         (ada-mode . lsp)\n")
            s.write("         ;; if you want which-key integration\n")
            s.write("         ;(lsp-mode . lsp-enable-which-key-integration)\n")
            s.write("        )\n")
            s.write("  :commands lsp)\n")
            s.write("\n")
            s.write("(add-hook 'ada-mode-hook #'lsp)\n")
            s.write(";; optionally\n")
            s.write("(use-package lsp-ui :commands lsp-ui-mode)\n")
            s.write(";;\n")
            s.write("(require 'company)\n")
            s.write("(add-hook 'after-init-hook 'global-company-mode)\n")
            s.write("\n")
            s.write("(require 'lsp-ada)\n")
            s.write("(global-set-key (kbd \"<backtab>\") 'company-complete)\n")

        startScript = os.path.join(self.destination, self.projectName+"_emacs.sh")
        with open(startScript, "w") as s:
            s.write("#!/bin/bash\n")
            for k,v in self.env.items():
                if not k in ENV_BLACKLIST:
                    s.write("export " +k + "=\"" + v + "\"\n")

            s.write("export GPR_PROJECT_PATH=" + ":".join(self.gprProjectPath) + "\n")
            s.write("PATH=${PATH}:"+self.gnatToolchain + " \\\n")
            s.write("emacs -l " + os.path.join(os.getcwd(),self.destination, self.projectName+".emacs $@\n"))

        st = os.stat(startScript)
        os.chmod(startScript, st.st_mode | stat.S_IEXEC)

def als_emacs(package, argv, extra, bobRoot):
    generator = EmacsGenerator()
    generator.configure(package, argv)
    generator.generate(extra, bobRoot)

class nvimGenerator(GeneratorAda):
    def __init__(self):
        super().__init__("als_nvim")

    def configure(self, packages, argv):
        super().configure(packages, argv)

    def generate(self, extra, bobRoot):
        super().generate(extra, bobRoot)

        projects = {
            name : Project(Path(os.getcwd()), scan)
            for name,scan in self.packages.items()
        }

        with open(os.path.join(self.destination, self.projectName+".nvim"), "w") as s:
            s.write(":lua << EOF\n")
            s.write("require('lspconfig').als.setup {\n")
            s.write("  cmd = { \"" + os.path.join(self.ada_language_server,"ada_language_server") + "\" };\n")
            s.write("  settings = {\n")
            s.write("    ada = {\n")
            s.write("      projectFile = \"" + self.defaultGprPath + "\";\n")
            s.write("      scenarioVariables = {\n");
            s.write("      };\n")
            s.write("    };\n")
            s.write("  };\n")
            s.write("}\n")
            s.write("EOF\n")

        startScript = os.path.join(self.destination, self.projectName+"_nvim.sh")
        with open(startScript, "w") as s:
            s.write("#!/bin/bash\n")
            for k,v in self.env.items():
                if not k in ENV_BLACKLIST:
                    s.write("export " +k + "=\"" + v + "\"\n")

            s.write("export GPR_PROJECT_PATH=" + ":".join(self.gprProjectPath) + "\n")
            s.write("PATH=\"${PATH}:"+self.gnatToolchain + "\" \\\n")
            s.write("nvim -S " + os.path.join(os.getcwd(),self.destination, self.projectName+".nvim $@\n"))

        st = os.stat(startScript)
        os.chmod(startScript, st.st_mode | stat.S_IEXEC)

def als_nvim(package, argv, extra, bobRoot):
    generator = nvimGenerator()
    generator.configure(package, argv)
    generator.generate(extra, bobRoot)

class vimGenerator(GeneratorAda):
    def __init__(self):
        super().__init__("als_vim")

    def configure(self, packages, argv):
        super().configure(packages, argv)

    def generate(self, extra, bobRoot):
        super().generate(extra, bobRoot)

        projects = {
            name : Project(Path(os.getcwd()), scan)
            for name,scan in self.packages.items()
        }

        with open(os.path.join(self.destination, self.projectName+".vim"), "w") as s:
            s.write("au User lsp_setup call lsp#register_server({\n")
            s.write("   \ 'name': 'ada_language_server',\n")
            s.write("   \ 'cmd': ['ada_language_server'],\n")
            s.write("   \ 'allowlist': ['ada'],\n")
            s.write("   \ 'workspace_config': {'ada': {\n")
            s.write("   \    'projectFile': \"" + self.defaultGprPath + "\"}},\n")
            s.write("   \ })\n")

        startScript = os.path.join(self.destination, self.projectName+"_vim.sh")
        with open(startScript, "w") as s:
            s.write("#!/bin/bash\n")
            for k,v in self.env.items():
                if not k in ENV_BLACKLIST:
                    s.write("export " +k + "=\"" + v + "\"\n")

            s.write("export GPR_PROJECT_PATH=" + ":".join(self.gprProjectPath) + "\n")
            s.write("PATH+=\":"+self.gnatToolchain + "\"\n")
            s.write("PATH+=\":"+self.ada_language_server + "\"\n")
            s.write("vim -S " + os.path.join(os.getcwd(),self.destination, self.projectName+".vim $@\n"))

        st = os.stat(startScript)
        os.chmod(startScript, st.st_mode | stat.S_IEXEC)

def als_vim(package, argv, extra, bobRoot):
    generator = vimGenerator()
    generator.configure(package, argv)
    generator.generate(extra, bobRoot)

class gnatStudioGenerator(GeneratorAda):
    def __init__(self):
        super().__init__("gnatstudio")

    def configure(self, packages, argv):
        super().configure(packages, argv)

    def generate(self, extra, bobRoot):
        super().generate(extra, bobRoot)

        projects = {
            name : Project(Path(os.getcwd()), scan)
            for name,scan in self.packages.items()
        }

        startScript = os.path.join(self.destination, "start_gnatstudio.sh")
        ENV_BLACKLIST.append("LARGS_AS_STRING")
        with open(startScript, "w") as s:
            s.write("#!/bin/bash\n")
            for k,v in self.env.items():
                if not k in ENV_BLACKLIST:
                    s.write("export " +k + "=\"" + v + "\"\n")

            s.write("export GPR_PROJECT_PATH=\"" + ":".join(self.gprProjectPath) + "\"\n")
            s.write("PATH=\""+self.gnatToolchain + ":" +  self.gprbuild + ":${PATH}\"\n")
            s.write("gnatstudio -P" + self.defaultGprPath + " \\\n")
            s.write("   --relocate-build-tree=" + os.path.join(os.path.abspath (self.destination), "build_gnatstudio/") +" \\\n")
            s.write('   --root-dir=${PWD} \\\n')
            s.write('   -Xeditor_mode=gps \\\n')
            s.write("   -Xeditor_out_dir="  + os.path.join(os.path.abspath (self.destination), "build_gnatstudio/"))

        with open(os.path.join(self.destination, "additional_buildargs.txt"), "w") as s:
            s.write("--RTS=" + self.rts)
            s.write(" -largs " + (self.env.get("LARGS_AS_STRING") if self.env.get("LARGS_AS_STRING") else '') + "\n")

        st = os.stat(startScript)
        os.chmod(startScript, st.st_mode | stat.S_IEXEC)

def als_gnatstudio(package, argv, extra, bobRoot):
    generator = gnatStudioGenerator()
    generator.configure(package, argv)
    generator.generate(extra, bobRoot)

manifest = {
    'apiVersion' : "0.22",
    'projectGenerators' : {
        'als_vscode' : {
            'func' : als_vscode,
            'query' : True
        },
        'als_emacs' : {
            'func' : als_emacs,
            'query' : True
        },
        'als_nvim' : {
            'func' : als_nvim,
            'query' : True
        },
        'als_vim' : {
            'func' : als_vim,
            'query' : True
        },
        'gnatstudio' : {
            'func' : als_gnatstudio,
            'query' : True
        }
    }
}
