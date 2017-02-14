from rez.config import config
from rez.packages_ import Package
from rez.serialise import load_from_file, FileFormat
from rez.packages_ import create_package
from rez.exceptions import PackageMetadataError, InvalidPackageError
from rez.utils.syspath import add_sys_paths
from rez.utils.sourcecode import SourceCode
from rez.utils.logging_ import print_info, print_error
from inspect import isfunction
import os.path


class DeveloperPackage(Package):
    """A developer package.

    This is a package in a source directory that is subsequently built or
    released.
    """
    def __init__(self, resource):
        super(DeveloperPackage, self).__init__(resource)
        self.filepath = None

        # include modules, derived from any present @include decorators
        self.includes = None

    @classmethod
    def from_path(cls, path):
        """Load a developer package.

        A developer package may for example be a package.yaml or package.py in a
        user's source directory.

        Args:
            path: Directory containing the package definition file.

        Returns:
            `Package` object.
        """
        name = None
        data = None

        for name_ in config.plugins.package_repository.filesystem.package_filenames:
            for format_ in (FileFormat.py, FileFormat.yaml):
                filepath = os.path.join(path, "%s.%s" % (name_, format_.extension))

                if os.path.isfile(filepath):
                    with add_sys_paths(config.package_definition_build_python_paths):
                        data = load_from_file(filepath, format_)
                    break
            if data:
                name = data.get("name")
                if name is not None or isinstance(name, basestring):
                    break

        if data is None:
            raise PackageMetadataError("No package definition file found at %s" % path)

        if name is None or not isinstance(name, basestring):
            raise PackageMetadataError(
                "Error in %r - missing or non-string field 'name'" % filepath)

        package = create_package(name, data, package_cls=cls)

        # postprocessing
        result = package._get_postprocessed(data)

        if result:
            package, data = result

        package.filepath = filepath

        # find all includes, this is needed at install time to copy the right
        # py sourcefiles into the package installation
        package.includes = set()

        def visit(d):
            for k, v in d.iteritems():
                if isinstance(v, SourceCode):
                    package.includes |= (v.get_includes() or set())
                elif isinstance(v, dict):
                    visit(v)

        visit(data)

        package._validate_includes()

        return package

    def _validate_includes(self):
        if not self.includes:
            return

        definition_python_path = self.config.package_definition_python_path

        if not definition_python_path:
            raise InvalidPackageError(
                "Package %s uses @include decorator, but no include path "
                "has been configured with the 'package_definition_python_path' "
                "setting." % self.filepath)

        for name in self.includes:
            filepath = os.path.join(definition_python_path, name)
            filepath += ".py"

            if not os.path.exists(filepath):
                raise InvalidPackageError(
                    "@include decorator requests module '%s', but the file "
                    "%s does not exist." % (name, filepath))

    def _get_postprocessed(self, data):
        """
        Returns:
            (DeveloperPackage, new_data) 2-tuple IFF the postprocess function
            changed the package; otherwise None.
        """
        from rez.serialise import process_python_objects
        from rez.utils.data_utils import get_dict_diff
        from copy import deepcopy

        with add_sys_paths(config.package_definition_build_python_paths):
            postprocess = getattr(self, "postprocess", None)

            if postprocess:
                postprocess_func = postprocess.func
                print_info("Applying postprocess from package.py")
            else:
                # load globally configured postprocess function
                dotted = self.config.package_postprocess_function

                if not dotted:
                    return None

                if '.' not in dotted:
                    print_error(
                        "Setting 'package_postprocess_function' must be of "
                        "form 'module[.module.module...].funcname'. Package  "
                        "postprocessing has not been applied.")
                    return None

                name, funcname = dotted.rsplit('.', 1)

                try:
                    module = __import__(name=name, fromlist=[funcname])
                except Exception as e:
                    print_error("Failed to load postprocessing function '%s': %s"
                                % (dotted, str(e)))
                    return None

                setattr(module, "InvalidPackageError", InvalidPackageError)
                postprocess_func = getattr(module, funcname)

                if not postprocess_func or not isfunction(isfunction):
                    print_error("Function '%s' not found" % dotted)
                    return None

                print_info("Applying postprocess function %s" % dotted)

            postprocessed_data = deepcopy(data)

            # apply postprocessing
            try:
                postprocess_func(this=self, data=postprocessed_data)
            except InvalidPackageError:
                raise
            except Exception as e:
                print_error("Failed to apply postprocess: %s: %s"
                            % (e.__class__.__name__, str(e)))
                return None

        # if postprocess added functions, these need to be converted to
        # SourceCode instances
        postprocessed_data = process_python_objects(postprocessed_data)

        if postprocessed_data == data:
            return None

        # recreate package from modified package data
        package = create_package(self.name, postprocessed_data,
                                 package_cls=self.__class__)

        # print summary of changed package attributes
        added, removed, changed = get_dict_diff(data, postprocessed_data)
        lines = ["Package attributes were changed in post processing:"]

        if added:
            lines.append("Added attributes: %s"
                         % ['.'.join(x) for x in added])
        if removed:
            lines.append("Removed attributes: %s"
                         % ['.'.join(x) for x in removed])
        if changed:
            lines.append("Changed attributes: %s"
                         % ['.'.join(x) for x in changed])

        txt = '\n'.join(lines)
        print_info(txt)

        return package, postprocessed_data