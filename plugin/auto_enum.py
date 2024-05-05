import os.path
import logging
import re
import traceback
import functools
import string
import json

import idc
import idautils
import idaapi
import ida_typeinf
import ida_nalt
import ida_hexrays
import ida_funcs

# From https://github.com/tmr232/Sark/blob/main/sark/ui.py#L358
class ActionHandler(idaapi.action_handler_t):
    """A wrapper around `idaapi.action_handler_t`.

    The class simplifies the creation of UI actions in IDA >= 6.7.

    To create an action, simply create subclass and override the relevant fields
    and register it::

        class MyAction(ActionHandler):
            TEXT = "My Action"
            HOTKEY = "Alt+Z"

            def _activate(self, ctx):
                idaapi.msg("Activated!")

        MyAction.register()

    Additional Documentation:
        Introduction to `idaapi.action_handler_t`:
            http://www.hexblog.com/?p=886

        Return values for update (from the SDK):
            AST_ENABLE_ALWAYS     // enable action and do not call action_handler_t::update() anymore
            AST_ENABLE_FOR_IDB    // enable action for the current idb. Call action_handler_t::update() when a database is opened/closed
            AST_ENABLE_FOR_WIDGET // enable action for the current widget. Call action_handler_t::update() when a form gets/loses focus
            AST_ENABLE            // enable action - call action_handler_t::update() when anything changes

            AST_DISABLE_ALWAYS    // disable action and do not call action_handler_t::action() anymore
            AST_DISABLE_FOR_IDB   // analog of ::AST_ENABLE_FOR_IDB
            AST_DISABLE_FOR_WIDGET// analog of ::AST_ENABLE_FOR_WIDGET
            AST_DISABLE           // analog of ::AST_ENABLE
    """
    NAME = None
    TEXT = "Default. Replace me!"
    HOTKEY = ""
    TOOLTIP = ""
    ICON = -1

    @classmethod
    def get_name(cls):
        """Return the name of the action.

        If a name has not been set (using the `Name` class variable), the
        function generates a name based on the class name and id.
        :return: action name
        :rtype: str
        """
        if cls.NAME is not None:
            return cls.NAME

        return "{}:{}".format(cls.__name__, id(cls))

    @classmethod
    def get_desc(cls):
        """Get a descriptor for this handler."""
        name = cls.get_name()
        text = cls.TEXT
        handler = cls()
        hotkey = cls.HOTKEY
        tooltip = cls.TOOLTIP
        icon = cls.ICON
        action_desc = idaapi.action_desc_t(
            name,
            text,
            handler,
            hotkey,
            tooltip,
            icon,
        )
        return action_desc

    @classmethod
    def register(cls):
        """Register the action.

        Each action MUST be registered before it can be used. To remove the action
        use the `unregister` method.
        """
        action_desc = cls.get_desc()

        return idaapi.register_action(action_desc)

    @classmethod
    def unregister(cls):
        """Unregister the action.

        After unregistering the class cannot be used.
        """
        idaapi.unregister_action(cls.get_name())

    def __init__(self):
        idaapi.action_handler_t.__init__(self)

    def activate(self, ctx):
        try:
            self._activate(ctx)
            return 1
        except:
            trace = traceback.format_exc()
            idaapi.msg("Action {!r} failed to activate. Traceback:\n{}".format(self.get_name(), trace))
            return 0

    def update(self, ctx):
        """Update the action.

        Optionally override this function.
        See IDA-SDK for more information.
        """
        return idaapi.AST_ENABLE_ALWAYS

    def _activate(self, ctx):
        """Activate the action.

        This function contains the action code itself. You MUST implement
        it in your class for the action to work.

        Args:
            ctx: The action context passed from IDA.
        """
        raise NotImplementedError()


class Argument:

    def __init__(self):
        self.name = ""
        self.enum = None
        self._logger = logging.getLogger(
            __name__ + '.' + self.__class__.__name__)


class Function:

    def __init__(self):
        self.name = ""
        self.arguments = []
        self._logger = logging.getLogger(
            __name__ + '.' + self.__class__.__name__)

    def __str__(self):
        return ("%s -- %s" % (self.name, self.arguments))

    def __repr__(self):
        return self.__str__()


def all_digits(val: str):
    return all(x in string.digits for x in val)


class FunctionMap:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.enums = json.loads(open(f"{self.data_dir}/enums.json").read())

    @functools.lru_cache()
    def __contains__(self, funcname: str):
        return os.path.exists(f"{self.data_dir}/functions/{funcname}.json")

    @functools.lru_cache()
    def __getitem__(self, funcname: str):
        if funcname not in self:
            raise KeyError(f"{funcname} not found!")
        func = Function()
        with open(f"{self.data_dir}/functions/{funcname}.json", "r") as file:
            data = json.loads(file.read())
            func.name = data["name"]
            args = []
            for k, v in data["enums"].items():
                arg = Argument()
                arg.name = k
                arg.enum = v
                args.append(arg)

            func.arguments = args
            return func

    def expand_enum(self, enum: dict[str, int], enum_id: str) -> dict[str, int]:
        items = list(enum.items())
        if not all_digits(enum_id):
            if re.search(r"_[0-9]+$", enum_id):
                enum_id = re.sub(r"_[0-9]+$", "", enum_id)
            for k, v in items:
                del enum[k]
                if k == "0":
                    enum["NULL"] = v
                else:
                    enum[f"{enum_id}_{k}"] = v
            return enum

        for k, v in items:
            if k == "0":
                del enum[k]
                enum["NULL"] = v
        return enum

    def get_enum(self, name: str):
        enum = self.enums[name]
        return self.expand_enum(enum, name)


def make_import_names_callback(library_calls, library_addr):
    """ Return a callback function used by idaapi.enum_import_names(). """

    def callback(ea, name, ordinal):
        """ Callback function to retrieve code references to library calls. """
        if "@" in name:
            name = name.split("@")[0]
            ea = next(idautils.CodeRefsTo(ea, 0), None)
            if ea is None:
                return True
            ea = next(ida_funcs.get_func(ea).addresses())
        library_calls[name] = []
        library_addr[name] = ea
        for ref in idautils.CodeRefsTo(ea, 0):
            library_calls[name].append(ref)
        return True  # True -> Continue enumeration

    return callback


def get_imports(library_calls, library_addr):
    """ Populate dictionaries with import information. """
    import_names_callback = make_import_names_callback(library_calls,
                                                       library_addr)
    for i in range(0, idaapi.get_import_module_qty()):
        idaapi.enum_import_names(i, import_names_callback)


def get_funcinfo(funcptr_addr):
    tif = ida_typeinf.tinfo_t()
    funcdata = ida_typeinf.func_type_data_t()

    if not ida_nalt.get_tinfo(tif, funcptr_addr):
        return None, None
    if not tif.is_funcptr():
        if tif.is_func():
            tif.get_func_details(funcdata)
            return False, funcdata
        return None, None
    if not tif.get_pointed_object().get_func_details(funcdata):
        return None, None
    return True, funcdata


@functools.lru_cache()
def get_or_add_enum(funcmap: FunctionMap, enum_id: str):
    enum_name = f"ENUM_{enum_id}"
    ida_enum_id = idc.get_enum(enum_name)
    if ida_enum_id == idaapi.BADADDR:
        ida_enum_id = idc.add_enum(-1, enum_name, idaapi.hex_flag())
        enum = funcmap.get_enum(enum_id)
        for k, v in enum.items():
            idc.add_enum_member(ida_enum_id, k, v, -1)
        return enum_name
    return enum_name

class Hooks(idaapi.UI_Hooks):
    def finish_populating_widget_popup(self, form, popup):
        type = idaapi.get_widget_type(form)
        if type == idaapi.BWN_DISASM or type == idaapi.BWN_PSEUDOCODE:
            idaapi.attach_action_to_popup(form, popup, AutoEnum.get_name(), '')

class AutoEnum(ActionHandler):
    TEXT = "Auto Enum"
    HOTKEY = "Ctrl+Shift+M"

    def _activate(self, ctx):
        main()


class AutoEnumPlugin(idaapi.plugin_t):
    flags = 0
    comment = 'Automatically detect standard enums'
    help = 'Automatically detect standard enums'
    wanted_name = 'Auto Enum'
    wanted_hotkey = "Ctrl+Shift+M"

    def init(self):
        print("[AutoEnum] Plugin loaded!")
        AutoEnum.register()
        self.hooks = Hooks()
        self.hooks.hook()

        return idaapi.PLUGIN_KEEP
    
    def term(self):
        self.hooks.unhook()
        AutoEnum.unregister()
    
    def run(self, arg):
        pass

def PLUGIN_ENTRY():
    return AutoEnumPlugin()


def main():
    handle = ida_hexrays.open_pseudocode(idc.here(), 0)
    library_calls = {}
    library_addr = {}
    get_imports(library_calls, library_addr)
    binary_type = "windows"
    if "ELF" in idaapi.get_file_type_name():
        binary_type = "linux"
    if binary_type == "windows":
        raise Exception("Windows is not supported at the moment!")
    thisdir = os.path.dirname(__file__)
    func_map = FunctionMap(os.path.join(thisdir, "data", binary_type))
    functions = list(library_addr.items())
    BOOL = ida_typeinf.tinfo_t()
    BOOL.get_named_type(idaapi.get_idati(), "MACRO_BOOL")
    for name, addr in functions:
        is_ptr, funcdata = get_funcinfo(addr)
        if name[:-1] in func_map:
            library_addr[name[:-1]] = library_addr[name]
            name = name[:-1]
        in_map = name in func_map
        changed = False
        if not funcdata:
            continue
        for arg in funcdata:
            if arg.type.is_ptr():
                continue
            type_name = arg.type.get_type_name()
            if type_name is not None and type_name.lower() in ["bool"]:
                arg.type = BOOL
                changed = True
            elif in_map and arg.type.is_integral() and not arg.type.is_enum():
                func = func_map[name]
                matching_args = [a for a in func.arguments if a.name == arg.name]
                if len(matching_args) and matching_args[0].enum is not None:
                    enum_name = get_or_add_enum(func_map, matching_args[0].enum)
                    enum_type = ida_typeinf.tinfo_t()
                    enum_type.get_named_type(idaapi.get_idati(), enum_name)
                    arg.type = enum_type
                    changed = True
        if changed:
            print(f"Setting enums for {name}")
            ti = idaapi.tinfo_t()
            ti.create_func(funcdata)
            if is_ptr:
                tip = idaapi.tinfo_t()
                tip.create_ptr(ti)
                ida_typeinf.apply_tinfo(addr, tip, idaapi.TINFO_DEFINITE)
            else:
                ida_typeinf.apply_tinfo(addr, ti, idaapi.TINFO_DEFINITE)
    handle.refresh_view(True)