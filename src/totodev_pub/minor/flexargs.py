# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

# flexargs.py

from typing import Dict, Sequence, Any, Optional,Union,Callable
from collections import defaultdict
import re
import sys
import os

class FlexArgs:
    """
    Utility class to be used in command line programs to provide flexible argument handling.

    Uses a non-standard approach to command line arguments using rules below.
        Items with one or two dashes in front of them are "switches".
        Switches with one dash may only be one character in length.
        The position of switches within the argument list is ignored.
        Arguments that are not switches are considered "positional" arguments.
        Positional arguments are numbered starting with 1. (NOT ZERO BASED!)
        Any argument or switch in square brackets is assumed to be optional otherwise it is required.
        Optional positional arguments may not appear after required positional arguments.
        The dictionary returned will respond with None for any key or switch that is not present.
        The last positional argument may optionally be an three dot ellipsis ('...'), indicating infinite optional positional arguments.

    It's important to note that throughout this class, the zeroeth argument is ignored (it is presumed to be the name of the program).    

    """
    def __init__(self, arg_spec: Union[str,Dict[str, str]], prog_purpose: str, extra_info: Optional[Dict[str,str]] = None) -> None:
        """
        Initializes the FlexArgs object with argument descriptions, program purpose, and extra program information.
        IMPORTANT NOTE: Order within the arg_spec dictionary is preserved but only for positional arguments.
        Logically speaking, non-value switches must be specified as optional.
        Note that extra_info is merged into the usage text with section title = key and section body = value

        Note that there are two special keys that may be used in the arg_spec dictionary:
        '[...]' - indicates that there may be an infinite number of optional positional arguments
        '[--...]' - indicates that there may be an infinite number of optional switches
        
        The following would be a valid arg_spec:
        {
            '[-s]': 'use silent mode',
            '[-x]': 'use extended mode',
            '-b=': 'byte count for the file (required)',
            '[--log]': 'log activity, default to no logging',
            '--limit=': 'limit the number of files to process', # required because no default or square brackets
            '--min=6': 'specify the minimum value' ,   # optional, causes a default value of 6 to be use if not specified
            '[--channel=]': 'specify the channel to use',
            '[--...=]: 'specify any merge variables',
            'output_file': 'specify output file to generate',
            'input_file': 'input file to process',
            '[...]': 'Additional input files may be specified',
        }

        Note that if arg_spec is passed as a simple string, then the arg_spec structure will be inferred from the string.
        The string must consist only of arg names and switch names (potentially in square brackets) separated by spaces.

        The above structure would allow FlexArgs to process a command line that looked like this:
        myprogram.py -s -b=1024 --log --limit=10 "--channel=g home" output_file input_file1 input_file2 input_file3

        """
        # Save the simple stuff
        self.prog_purpose = prog_purpose
        self.extra_info = extra_info
        self.suppress_help = False # if true, suppresses --help and --h handling in startup_main()
        # Pre-clear the cached argv list and args_dict
        self._argv = None
        self._args_dict = None # type: Dict[str,str]
        
        # Now a massive effort to interpret and save the arg_spec dictionary
        if isinstance(arg_spec, str): # for lazy users, allow arg_spec to be a string
            arg_spec = self.__class__.deduce_arg_spec(arg_spec)
        elif not isinstance(arg_spec, dict):
            raise ValueError("arg_spec must be a dictionary or a string")
        # Update the keys in arg_spec to mark switches as optional if they don't end in equal signs because keys are inherently optional
        self._arg_spec = { (f"[{key}]" if key.startswith('-') and not key.endswith('=') else key) : value for key, value in arg_spec.items()}
        self._default_values = {} 
        # Extract default value assignment from the keys and store them in a separate dictionary
        remove_keys = []
        add_items = {}
        for key,descr in self._arg_spec.items():
            if '=' in key and not (key.endswith('=') or key.endswith('=]')):
                sw = self.__class__.strip_optional(key)
                sw_name = self.__class__.strip_to_switch_name(sw)
                self._default_values[sw_name] = sw[len(sw_name):]
                remove_keys.append(key)
                reduced_key = key[(1 if key[0] == '[]' else 0):key.find('=')+1] + (']' if key.endswith(']') else '') 
                add_items[reduced_key] = descr
        for key in remove_keys:
            del self._arg_spec[key]
        self._arg_spec.update(add_items)


        self._switch_argspec_names, self._pos_argspec_names = self.__class__.partition_argspec(arg_spec.keys())

        # find the first entry in self.pos_arg_names that is optional and confirm that all subsequent entries are optional
        # if not, raise an exception
        optional_found = False
        for arg in self._pos_argspec_names:
            if arg == '...' or arg == '--...':
                raise ValueError(f"Ellipses argument must be in square brackets to indicate optional '[...]' or '[--...]'")
            if arg.startswith('['):
                optional_found = True
            elif optional_found:
                raise ValueError(f"Optional positional arguments must appear after required positional arguments {self._pos_argspec_names}")
        

        # strip the square brackets from the name lists
        self._pos_argspec_names = [self.__class__.strip_optional(arg) for arg in self._pos_argspec_names]   
        self._switch_argspec_names = [self.__class__.strip_to_switch_name(arg) for arg in self._switch_argspec_names] 

        self._required_pos_arg_count = sum(1 for arg in self._pos_argspec_names if arg in self._arg_spec)

        # Verify format of switch names (contains only letters, numbers, and underscores)
        for arg in self._switch_argspec_names:
            if not(arg == '--...' or re.match(r'^--?[a-zA-Z0-9_][a-zA-Z0-9_-]*=?.*$', arg)):
                raise ValueError(f"Command line switch name '{arg}' contains invalid characters.  Only letters, numbers, internal hyphens, and underscores are allowed.")
            if re.match(r'^-[a-zA-Z0-9_][a-zA-Z0-9_-]{1,}=?$', arg):
                raise ValueError(f"Single character switches may only be one character long.  '{arg}' is too long.")    
        for arg in self._pos_argspec_names:
            if not(arg == '...' or re.match(r'^[a-zA-Z0-9_][a-zA-Z0-9_-]*$', arg)):
                raise ValueError(f"Command line positional argument name '{arg}' contains invalid characters.  Only letters, numbers, internal hyphens, and underscores are allowed.") 


        # verify that if '...' is in the pos_argspec_names list, it is the last entry
        if '...' in self._pos_argspec_names and self._pos_argspec_names[-1] != '...':
                raise ValueError(f"If specified, the ellipses argument must be last '[...]'")

    # Create a readable property with a list of required switch names

    def get_arg_description(self, argname: str) -> Union[str,None]:
        """
        Returns the description of the given argument name.
        """
        return self._arg_spec.get(argname, None) or self._arg_spec.get(f"[{argname}]", None) 

    @property
    def required_switch_names(self) -> Sequence[str]:
        """
        Returns a list of required switch names.
        """
        return [arg for arg in self._switch_argspec_names if arg in self._arg_spec]

    @property
    def required_pos_arg_names(self) -> Sequence[str]:
        """
        Returns a list of required positional argument names.
        """
        return [arg for arg in self._pos_argspec_names if arg in self._arg_spec]

    @property
    def optional_switch_names(self) -> Sequence[str]:
        """
        Returns a list of optional switch names.
        """
        return [arg for arg in self._switch_argspec_names if arg not in self._arg_spec]
    
    @property
    def optional_pos_arg_names(self) -> Sequence[str]:
        """
        Returns a list of optional positional argument names.
        """
        return [arg for arg in self._pos_argspec_names if arg not in self._arg_spec]

    def startup_main(self,extra_dict_transformer: Optional[Callable] = None, err_abort_code = -1,simplify_key_names=False) -> None:
        """
        Conveniently handles the startup of a command line program:
            * Get argv list from sys.argv
            * If --help of -h is present, print usage and exit(0)
            * If there are any deviations in the argv list, print to stderr and exit(err_abort_code)
               * note that if err_abort_code is None, no exit will occur and an error will be thrown
            * return the result of as_dict() including any extra_dict_transformer() function
               * If errors raised, allow unhandled exception to propagate
            
        Programmers can use this method upon immediate startup of their program for compact convenience.
        ```
        def transform_args(args_dict):
            args_dict['max_count'] = int(args_dict['max_count'])
            return altered_args_dict

        if __name__ == "__main__":
            startup_params = FlexArgs(arg_spec, "Program Purpose").startup_main(transform_args)
        ```
        """
        if self._argv is None:  # grab system args if they haven't already been set
            self.set_argv(sys.argv)
        if (not self.suppress_help) and ('--help' in self._argv or '-h' in self._argv):
            print(self.usage_body())
            exit(0)
        errors = self.argv_deviations()
        if not errors:
            # Currently no handling on errors because we can't be detailed and credible enough
            return self.as_dict(extra_dict_transformer=extra_dict_transformer,simplify_key_names=simplify_key_names)

        if err_abort_code is None:
            for error in errors:
                print(f"Error processing command line arguments: {error}", file=sys.stderr)
            exit(err_abort_code)
        else:
            raise ValueError("\n".join([
                "Error processing command line arguments:",
                *[f"  - {error}" for error in errors]
            ]))
        
        

    @staticmethod
    def deduce_arg_spec(arg_spec_str: str) -> Dict[str, str]:
        """
        Deduces the arg_spec dictionary from a string of arg names and switch names.
        The string must consist only of arg names and switch names (potentially in square brackets) separated by spaces.
        """
        arg_spec = {}
        for arg in arg_spec_str.split():
            arg_spec[arg] = f"Description of {arg}"
        return arg_spec

    @staticmethod
    def is_switch(actual_arg):
        return False if (actual_arg == '-' or actual_arg == '--') else actual_arg.startswith('-')
        
    @staticmethod
    def is_optional_argspec(arg):
        return arg.startswith('[') and arg.endswith(']')
    
    @staticmethod
    def strip_optional(arg):
        return arg[1:-1] if __class__.is_optional_argspec(arg) else arg

    @staticmethod
    def strip_to_switch_name(arg):
        """Switch name includes dashes and equal sign but does not include value or square brackets"""
        arg = __class__.strip_optional(arg)
        return arg[:arg.find('=')+1] if '=' in arg else arg

    @staticmethod
    def partition_argspec(arg_spec):
        """
        Partitions the arg_spec dictionary into a list of switches and a list of positional arguments.
        The assumption is that arg_spec is a dictionary of strings.
        """
        switches = []
        pos_args = []
        for arg in arg_spec:
            # arg may be surrounded by square brackets, so extract the part between brackets using rege
            base_arg = arg if not arg.startswith('[') else re.search(r'^\[?(.*)\]?$', arg).group(1) 
            if FlexArgs.is_switch(base_arg):
                switches.append(arg)
            else:
                pos_args.append(arg)
        return switches, pos_args    
        
    @staticmethod
    def partition_args(actual_args):
        """
        Partitions the actual_args list into a list of switches and a list of positional arguments.
        The assumption is that actual_args is essentially a list of strings from sys.argv[1:]
        """
        switches = []
        pos_args = []
        for arg in actual_args:
            if FlexArgs.is_switch(arg):
                switches.append(arg)
            else:
                pos_args.append(arg)
        return switches, pos_args

    def set_sys_argv(self) -> 'FlexArgs':
        """
        Sets the argv list for the FlexArgs object to the system argv list.
        """
        return self.set_argv(sys.argv)

    def set_argv(self, argv: Sequence[str]) -> 'FlexArgs':
        """
        Sets the argv list for the FlexArgs object.
        """
        self._argv = argv
        self._args_dict = None if self._argv is None else self.as_dict(argv)
        return self  # for convenience

    def __getitem__(self, key):
        """Allow subscript access to the values same as if user had called as_dict()"""
        if self._args_dict is None:
            raise ValueError("An argv list must be supplied or previously set via set_argv()")
        return self._args_dict[key]
        
    @staticmethod
    def deduce_arg_type_from_desc(desc: str) -> str:
        """
        Deduces the type of the argument from the description.
        """
        if re.search(r'must be( a| an)? (Integer|int)',desc,re.IGNORECASE):
            return 'int'
        elif re.search(r'must be( a| an)? (float|number)',desc,re.IGNORECASE):
            return 'float'
        elif re.search(r'must be one of \[([^\]]*)\]',desc,re.IGNORECASE):
            return 'enum'
        return 'string'

    def implied_arg_type(self, argname: str) -> str:
        """Deduce the type of the argument from the description.  If the argument is not found, return None."""
        descr = self._arg_spec.get(argname, None) or self._arg_spec.get(f"[{argname}]", None)   
        return self.__class__.deduce_arg_type_from_desc(descr)

    def usage_body(self,progname_if_unk = '__script__') -> str:
        """
        Constructs and returns the usage information text for the host program.

        The name of the program is taken from the zeroeth element of the argv list if set_argv() has been called.
        If set_argv() has not been called, the name of the program is assumed to be progname_if_unk.
        """
        program_name = os.path.basename(self._argv[0]) if self._argv else progname_if_unk
        switches,pos_args = self.partition_argspec(self._arg_spec)
        nl = "\n"
        option_lines = [f" {arg}{' '*(20-len(arg))}{self._arg_spec[arg]}" for arg in switches]
        parameter_lines = ['  None'] if not pos_args else [f"  {arg}{' '*(20-len(arg))}{self._arg_spec[arg]}" for arg in pos_args]
        extra_info_lines = []
        if self.extra_info:
            for heading,section in self.extra_info.items():
                extra_info_lines.append(f"{heading}:")
                for line in section.splitlines():
                    extra_info_lines.append(f"  {line}")
            
        usage = f"""
Usage: {program_name} {'[OPTIONS]' if switches else ''} {' '.join(pos_args)}

{self.prog_purpose}

Parameters:
{nl.join(parameter_lines)}

Options:{'' if self.suppress_help else nl +' -h, --help          Show this help message and exit'} 
{nl.join(option_lines)}

{nl.join(extra_info_lines)}
"""
        return usage
    
    def argv_deviations(self, argv: Sequence[str]= None) -> bool:
        """
        Examines the given argv list to detect a number of logical problems based on the valid arg_spec.
        * Too few positional arguments
        * Too many positional arguments (suppressed by presence of [...])
        * Required value-switches missing
        * Unknown switches present (suppressed by presence of [--...])
        * Validate switch and parameter values if description includes magic word patterns
           * 'must be a/an <type>' where type is ['int','Integer','float','number']   
           * 'must be one of [<list>]' where list is a comma separated list of values

        Return value is a list of strings describing the issues found.
        List is empty if there were no problems found.
        """
        args_dict = self.as_dict(argv)
        switches, pos_args = self.partition_argspec(args_dict)
        error_list = []

        # Validate positional argument quantity
        if len(pos_args) < self._required_pos_arg_count:
            error_list += [f"Too few positional arguments. Expected {self._pos_argspec_names}, found {pos_args}"]
        if len(pos_args) > len(self._pos_argspec_names) and '...' not in self._pos_argspec_names:
            error_list += [f"Too many positional arguments. Expected {self._pos_argspec_names}, found {args_dict['...']}"]

        # Validate switch quantity and presence
        switch_name_set = set(switches)
        # required switches are those that are not optional (not in square brackets in self.arg_spec)
        required_switch_set = set([arg for arg in self._switch_argspec_names if arg in self._arg_spec])
        # make a list of required switches that are not present
        missing_required_switches = required_switch_set - switch_name_set
        if missing_required_switches:         
            error_list += [f"Missing required switches: {missing_required_switches}"]
        if '[--...]' not in self._arg_spec:
            unrecognized_switches = [switch for switch in (switch_name_set-required_switch_set) if switch not in self._switch_argspec_names] 
            if unrecognized_switches:
                error_list += [f"Unrecognized optional switches: {unrecognized_switches}"] 

        # Validate value types
        must_be_in_list_rx = re.compile(r'must be one of \[([^\]]*)\]',re.IGNORECASE)
        for argname,descr in self._arg_spec.items():
            argname = self.__class__.strip_optional(argname)
            if argname not in args_dict:
                continue  #no need to validate if a value wasn't provided
            implied_arg_type = self.__class__.deduce_arg_type_from_desc(descr)
            if implied_arg_type == 'string':
                continue
            elif implied_arg_type == 'int':
                try:
                    int(args_dict[argname])
                except ValueError:
                    error_list += [f"Switch '{argname}{args_dict[argname]}' must be an Integer"]
            elif implied_arg_type == 'float':
                try:
                    float(args_dict[argname])
                except ValueError:
                    error_list += [f"Switch '{argname}{args_dict[argname]}' must be a number"]
            elif implied_arg_type == 'enum':
                valid_values = [x.strip() for x in must_be_in_list_rx.search(descr).group(1).split(',')]
                if args_dict[argname] not in valid_values:
                    error_list += [f"Switch '{argname}{args_dict[argname]}' must be one of {valid_values}"]

        return error_list


    def as_dict(self, argv: Sequence[str] = None, extra_dict_transformer: Optional[Callable] = None,simplify_key_names=False) -> Dict[str, Any]:
        """
        Converts the argv list into a dictionary with argument/switch names as keys.
        Note that this method does not raise errors.  It recognizes switches and 
        positional args as they appear in argv.  If there are more positional args
        then the arg_spec names, the extra args are placed in a list under the
        '...' key as a list.  Keep in mind that the value at position zero of
        the argv is presumed to be the name of the program and is ignored.

        myprog.py -s -x -b=valb --switch1 --switch2=val2 --switch3=val3 pos1 pos2 posoptX posoptY
        The above argv list would be converted to the following dictionary:
            {
                '-s': True,
                '-x': True,
                '-b': 'valb',
                '--switch1': True,
                '--switch2': 'val2',
                '--switch3': 'val3',
                1 : 'pos1',
                2 : 'pos2',
                '...': ['posoptX', 'posoptY']
            }

        Note that if it is provided with an extra_dict_transformer function, it will call that function with the resulting dictionary and return the result of that function.

        The simplify_key_names parameter will cause the keys to be stripped of their leading dashes and equal signs.
        """
        args = self._safe_args(argv)
        switches, pos_args = self.partition_args(args)

        # map positional arguments to their names (name becomes key)
        args_dict = defaultdict(lambda: None) # unknown keys return None
        ellipses_args = []
        named_arg_count = min(len(pos_args), len(self._pos_argspec_names))
        for i in range(0,len(pos_args)):
            if i < named_arg_count:
                args_dict[self._pos_argspec_names[i]] = pos_args[i]
            else:
                ellipses_args.append(pos_args[i])
        if ellipses_args:
            args_dict['...'] = ellipses_args
        
        # map switches to their values
        supplied_value_switches : set = set()
        for arg in switches:
            if '=' in arg:
                args_dict[arg[:arg.find('=')+1]] = arg[arg.find('=')+1:]
                #append just the switch name to the supplied_value_switches list
                supplied_value_switches.add(self.__class__.strip_to_switch_name(arg))
            else:
                args_dict[arg] = True # gets 'True' if no assignment appears

        # Apply any default values unless a value was specified
        for arg in self._default_values:
            if arg not in supplied_value_switches:
                args_dict[arg] = self._default_values[arg]

        # Apply any extra_dict_transformer function to mutate the dictionary
        if extra_dict_transformer:
            args_dict = extra_dict_transformer(args_dict)
            if not isinstance(args_dict, dict):
                raise ValueError("The extra_dict_transformer() Callable must return a dictionary") 

        if simplify_key_names:
            args_dict = {self.__class__.strip_to_switch_name(k): v for k,v in args_dict.items()}    

        return args_dict

    def positional_args(self, argv = None) -> int:
        """
        Returns the non-switch arguments in the argv list.
        """
        args = self._safe_args(argv)

        return [arg for arg in args if not self.__class__.is_switch(arg)]
    
    def _safe_args(self, argv = None) -> Dict[str,str]:
        """
        Returns the either the cache argv or the passed argv, witht the first element removed
        """
        argv = argv if argv else self._argv
        if argv is None:
            raise ValueError("An argv list must be supplied or previously set via set_argv()") 
        return argv[1:]

    def switches(self, argv = None) -> int:
        """
        Returns the switches in the argv list.
        """
        args = self._safe_args(argv)
        return [arg for arg in args if self.__class__.is_switch(arg)]



if __name__ == "__main__":
    if '--help' in sys.argv:
        print("")
        print("Use as command line program to generate sample 'arg_spec' for FlexArgs constructor:")
        print("Pass in the names of the arguments and options as command line arguments.  The description will be placeholders")
        print("")
    elif len(sys.argv) > 1:
        print("Python code snippet to pass as 'arg_spec' into the FlexArgs constructor:")
        print("arg_spec = {")
        for arg in sys.argv[1:]:
            print(f"    '{arg}': 'Description of {arg}',")
        print("}")
    else:
        print("Example of the sort of arg_spec that is valid to pass to the FlexArgs constructor:") 
        print("""\
    {
        '[-s]': 'use silent mode',
        '-x': 'use extended mode', # even though not in square brackets, boolean switches are optional
        '-b=': 'byte count for the file (required)',
        '[--log]': 'log activity, default to no logging',
        '--limit=': 'limit the number of files to process', # required because no default or square brackets
        '--min=6': 'specify the minimum value (must be integer)' ,   # optional, causes a default value of '6' if not specified
        '[--channel=]': 'specify the channel to use (must be one of [a,b,c])',
        '[--...=]: 'specify any merge variables', # allows open ended number of switches
        'output_file': 'specify output file to generate',
        'input_file': 'input file to process',
        '[...]': 'Additional input files may be specified', # allows open ended number of positional arguments
    }""")
