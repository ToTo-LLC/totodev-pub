# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

# pipeline_folder.py
# Used to track and manage a folder containing a pipeline instance.

"""
DEPRECATION NOTICE:

This DateTreeFolder class is being deprecated in favor of the date_folders.py module 
(and the DateFolders class within it),
which provides similar functionality using a much simpler structure.

This module lives under ``totodev_pub.minor``; prefer ``date_folders.DateFolders`` for new work.
Please migrate to using the date_folders.py module for new development.

For migration assistance, refer to the date_folders.py documentation.
"""

import os
import datetime
from typing import List, Union, Optional, ClassVar, Dict, Callable
from collections.abc import Generator
import random
import re
import click
from pathlib import Path

_ALPHANUMERIC_CHARS = "123456789ABCDEFGHIJKLMNPQRSTUVWXYZabcdefghijklmnpqrstuvwxyz" # for randomizing uniqueness

class DateTreeFolder:
    """
    This class is used to track and manage a date-structured tree of folders containing arbitrary data.
    It is designed to dynamically create new folders within this tree and allow for iterating through them.
    One of the easiest ways to use this class is to use DateTreeFolder.make_folder_factory() to create a factory function
    that can be used to create DateTreeFolder instances.
    
    While the exact structure is an internal implementation detail, Below is an example of the sort of structure:
    <ultimate_root>/
        2024-02/
            01-Mon/
                <category_name>/
                    <uniqueness_src1++>/
                    <uniqueness_src2++>/
                    ...
                    <uniqueness_srcN++>/
                    ...
            02-Tue/
                ...

    Users wanting to create one of these objects need to supply:
        - ultimate root (required)
        - date (if not today)
        - category name (required)
        - uniqueness source (optional string, will be generated from system time if not provided but may be some meaningful value suitable for folder naming)
                
    An instance of this class is bound to a single folder in the tree.
    Class methods allow for searching through date ranges and finding all folders that match a given category name.
    This class NEVER looks within the folder it creates as a strict policy.  
    But it does work to ensure folder names are unique by appending random characters when needed.

    This class is intended to handle low level details of folder management.
    It is strongly suggested that developers create something like a "Date Tree Factory" class
    that can simplify the creation of these objects for your use case.

    You can also use the class method `apply_retention_policy` to delete old folders.

    PERFORMANCE NOTE: The operating system and file system determine whether a strategy like this is efficient.
    Generally speaking, the *Nix file systems can perform well with up to 100,000 files in a single directory.
    But if you are doing something high volume, this may not be the tool for you and you may want to look into a database.
    """

    EXTRA_RANDOM_CHARS = 3  # base number of random chars added to end of folder name if uniqueness is not found
    RANDOM_CHAR_DELIM = "~"  # delimiter between the folder name and the random characters (if any)
    _last_retention_run_dates: ClassVar[Dict[str, datetime.date]] = {}  # Maps ultimate_root to last run date

    def __init__(self,
                 existing_folder_path: Optional[str] = None,
                 category_name: str = None,
                 uniqueness_src: Optional[str] = None,
                 ultimate_root: str = None,
                 dte: Optional[datetime.date] = None) -> None:
        """
        Initialize a DateTreeFolder instance that represents a folder in a date-structured tree.
        
        The folder structure follows the pattern: `<ultimate_root>/YYYY-MM/DD-Www/category/uniqueness`
        
        This class supports two initialization modes:
        
        1. Binding to an existing folder:
           - Provide only `existing_folder_path` to bind to an existing folder in the date tree
           - The path must match the expected structure and the folder must exist
           - All other parameters must be None in this mode
        
        2. Creating a new folder:
           - Provide `ultimate_root` and `category_name` (both required)
           - Optionally provide `uniqueness_src` (defaults to timestamp if None)
           - Optionally provide `dte` (defaults to today if None)
           - The folder will be created if it doesn't exist
           - Random characters may be appended to ensure uniqueness
        
        Args:
            existing_folder_path: Path to an existing folder to bind to. If provided, all other
                                 parameters must be None.
            category_name: Name of the category folder (required when creating a new folder).
                          Cannot be an absolute path.
            uniqueness_src: String used to make the folder name unique. If None, a timestamp
                           will be used. Only used when creating a new folder.
            ultimate_root: Root directory for the date tree structure. Required when creating
                          a new folder.
            dte: Date to use for the folder structure. Defaults to today if None.
                Only used when creating a new folder.
        
        Raises:
            ValueError: If parameters are inconsistent with the chosen initialization mode,
                       if required parameters are missing, or if the folder structure is invalid.
        
        Note:
            When creating a new folder, the directory will be automatically created if it
            doesn't already exist, including all necessary parent directories.
        """
        # Initialize all private attributes
        self._ultimate_root = None
        self._datetree_folder_path = None
        self._datetree_folder_abspath = None
        self._category_name = None
        self._uniqueness_src = None
        self._date = None

        if existing_folder_path is None and category_name is None:
            raise ValueError("Must provide either a category_name (to create new folder) or an existing_folder_path (to bind to existing folder)")

        if existing_folder_path and (dte or category_name or uniqueness_src or ultimate_root):
            raise ValueError("Cannot provide existing_folder_path and any of dte, category_name, uniqueness_src, or ultimate_root")

        if existing_folder_path is not None:
            if not os.path.exists(existing_folder_path):
                raise ValueError(f"Pipeline folder {existing_folder_path} does not exist")
            self._datetree_folder_path = existing_folder_path
            self._datetree_folder_abspath = os.path.abspath(existing_folder_path)
            
            # Fix: Look at correct path components
            split_path = self._datetree_folder_abspath.split(os.sep)
            if len(split_path) < 4:
                raise ValueError(f"Pipeline folder path {self._datetree_folder_abspath} is not well formed")
                
            # The path structure is <root>/YYYY-MM/DD-Www/category/uniqueness
            month_pattern = re.match(r"\d{4}-\d{2}", split_path[-4])  # YYYY-MM
            day_pattern = re.match(r"\d{2}-\w{3}", split_path[-3])    # DD-Www
            if not (month_pattern and day_pattern):
                raise ValueError(f"Pipeline folder path {self._datetree_folder_abspath} is not well formed.  Expected format: <root>/YYYY-MM/DD-Www/category/uniqueness")
            
            uniqueness_src_ext = split_path[-1]
            if self.RANDOM_CHAR_DELIM in uniqueness_src_ext:
                self._uniqueness_src = uniqueness_src_ext.split(self.RANDOM_CHAR_DELIM)[0]
            else:
                self._uniqueness_src = uniqueness_src_ext
            self._category_name = split_path[-2]
            # Fix: Parse date correctly from YYYY-MM and DD components
            month = split_path[-4]  # YYYY-MM
            day = split_path[-3][:2]  # DD from DD-Www
            self._date = datetime.date.fromisoformat(f"{month}-{day}")
            self._ultimate_root = os.path.join(*split_path[:-4])
        else:
            if ultimate_root is None:
                raise ValueError("ultimate_root must be provided when creating a new folder")
            if category_name is None:
                raise ValueError("category_name must be provided when creating a new folder")
            if os.path.isabs(category_name):
                raise ValueError("category_name cannot be an absolute path")
                
            self._ultimate_root = ultimate_root
            self._date = dte or datetime.date.today()
            path_parts = self._tentative_new_folder_path(category_name=category_name, ultimate_root=self._ultimate_root,
                                                        dte=self._date, uniqueness_src=uniqueness_src)
            base_path = os.path.join(*path_parts[:-1])
            os.makedirs(base_path, exist_ok=True)
            self._datetree_folder_path = self.__class__._mod_to_unique_dir(
                os.path.join(base_path, path_parts[-1]),
                uniqueness_src=uniqueness_src,
                make_dir=True
            )
            self._datetree_folder_abspath = os.path.abspath(self._datetree_folder_path)
            self._category_name = category_name
            self._uniqueness_src = uniqueness_src

    @property
    def ultimate_root(self) -> str:
        """ Returns the ultimate pipeline root folder path of this object. """
        return self._ultimate_root

    @property
    def abspath(self) -> str:
        """Returns the absolute path to the pipeline folder."""
        return self._datetree_folder_abspath
    
    @property
    def path(self) -> Path:
        """ Returns the path to the pipeline folder as a Path object. In many cases, same as abspath() but as a Path object. """
        return Path(self._datetree_folder_path)
    
    @property
    def category_name(self) -> str:
        """ Returns the category name of this pipeline folder. """
        return self._category_name
    
    @property
    def date(self) -> datetime.date:
        """ Returns the date of this pipeline folder. """
        return self._date
    
    @property
    def uniqueness_src(self) -> str:
        """ Returns the uniqueness source of this pipeline folder. """
        return self._uniqueness_src
    
    def _infer_self_from_path(self) -> None:
        """No longer needed - all attributes are set during initialization"""
        pass

    @classmethod
    def apply_retention_policy(cls, ultimate_root: str, retain_days: int, reference_date: datetime.date = None, force: bool = False) -> None:
        """
        Applies the retention policy to the folder structure in the given ultimate_root.
        
        This method deletes folders older than the retention period. To avoid unnecessary
        processing, it only runs once per day unless forced. This method is a no-op if
        retain_days is None or less than 0.
        
        Args:
            ultimate_root: The root directory containing the date tree structure.
            retain_days: Number of days to retain. Folders older than this will be deleted.
            reference_date: The date to use as reference for retention calculation. Defaults to today.
            force: If True, runs the retention policy even if it has already been run today.
                  Default is False.
        """
        # Skip if no retention policy is set or if already run today (unless forced)
        if retain_days is None or retain_days < 0:
            return  # no-op
            
        today = datetime.date.today()
        if not force and cls._last_retention_run_dates.get(ultimate_root) == today:
            return  # already run today
            
        reference_date = reference_date or today
        cutoff_date = reference_date - datetime.timedelta(days=retain_days)
        end_date = cutoff_date - datetime.timedelta(days=1)  # exclude the cutoff date itself
        
        # Use None for begin_date to get all dates from the beginning of time up to end_date
        cls.purge_by_date(
            begin_date=None,  # Start from earliest date
            ultimate_root=ultimate_root,
            end_date=end_date,  # Up to but not including cutoff_date
        )
        
        # Update the last run date
        cls._last_retention_run_dates[ultimate_root] = today

    @classmethod
    def types_on_date(cls, dte:datetime.date, ultimate_root:str) -> List[str]:
        """
        Returns a list of all category types that exist on the given date.
        Returns empty list if directory doesn't exist.
        """
        p = cls._tentative_new_folder_path(category_name="UNUSEDZZZ", ultimate_root=ultimate_root, dte=dte, uniqueness_src="UNUSEDZZZ")
        day_path = os.path.join(*p[:-2]) # remove the category type and uniqueness source
        try:
            return [os.path.basename(f) for f in os.listdir(day_path)]
        except FileNotFoundError:
            return []

    @classmethod
    def instances_on_date(cls, dte:datetime.date, category_name:str, ultimate_root:str) -> Generator[str, None, None]:
        """
        Returns a generator of all folder instances that exist on the given date.
        Yields nothing if directory doesn't exist.
        """
        p = cls._tentative_new_folder_path(category_name=category_name, ultimate_root=ultimate_root, dte=dte, uniqueness_src="UNUSEDZZZ")
        type_path = os.path.join(*p[:-1]) # remove the uniqueness source
        try:
            for f in os.listdir(type_path):
                yield os.path.basename(f)
        except FileNotFoundError:
            return

    @classmethod
    def make_folder_factory(cls, ultimate_root: str, category_name: str, retain_days: Optional[int] = None) -> Callable[[Optional[str]], "DateTreeFolder"]:
        """
        Creates and returns a factory function for generating DateTreeFolder instances.
        
        This factory function simplifies the creation of DateTreeFolder objects by pre-configuring
        common parameters. The returned factory will always use the current date (today) when creating
        folders.
        
        Args:
            ultimate_root: The root directory where the date tree structure will be created.
            category_name: The category name to use for all folders created by this factory.
            retain_days: Optional number of days to retain folders. If provided, the factory
                         will periodically call apply_retention_policy to clean up old folders.
                         
        Returns:
            callable[[Optional[str]], DateTreeFolder]: A factory function that takes an optional uniqueness_src parameter and
                     returns a new DateTreeFolder instance.
                     
        Example:
            ```python
            # Create a factory for log folders that keeps 30 days of history
            log_folder_factory = DateTreeFolder.make_folder_factory(
                                                    ultimate_root="/var/emails",
                                                    category_name="inbound",
                                                    retain_days=30
                                                )
            
            # Create a new log folder with default uniqueness
            inbound_folder_generator = log_folder_factory()
            
            # Create a log folder with custom uniqueness
            newmail_folder = inbound_folder_generator(uniqueness_src="joe@test.com")
            ```
        """
        def factory(uniqueness_src: Optional[str] = None) -> "DateTreeFolder":
            """
            Factory function that creates a new DateTreeFolder instance.
            
            Args:
                uniqueness_src: Optional string to use for uniqueness in folder naming.
                               If not provided, a timestamp-based value will be used.
                               
            Returns:
                DateTreeFolder: A new DateTreeFolder instance with the configured parameters.
            """
            if retain_days is not None:
                cls.apply_retention_policy(ultimate_root=ultimate_root, retain_days=retain_days)
            return cls(ultimate_root=ultimate_root, category_name=category_name, uniqueness_src=uniqueness_src)
        return factory

    @classmethod
    def purge_by_date(cls, begin_date:Optional[datetime.date], ultimate_root:str,
                      end_date:Optional[datetime.date]=None,
                      category_name:Optional[str]=None) -> None:
        """
        Deletes all day-level folders that are within the given date range (inclusive).
        
        This method removes all folders for dates between begin_date and end_date, including
        those dates themselves. After deletion, any empty year-month directories are also removed.
        
        Args:
            begin_date: Start date for deletion range. If None, will use the earliest date found
                in the directory structure.
            ultimate_root: Root directory containing the date tree structure.
            end_date: End date for deletion range. If None, defaults to today's date.
            category_name: Optional category name to filter by. If provided, only folders with
                this category name will be deleted.
            
        Note:
            This is a destructive operation that permanently removes data. Use with caution.
            No confirmation is requested before deletion.
        """
        if begin_date is None:
            # Get the first date from active_dates generator
            dates = list(cls.active_dates(None, ultimate_root, end_date=end_date, category_name=category_name))
            if not dates:
                return  # No dates found, nothing to purge
            begin_date = min(dates)
            
        if end_date is None:
            end_date = datetime.date.today()
            
        # Get all dates within the range
        dates_to_purge = set(cls.active_dates(begin_date, ultimate_root, end_date=end_date, category_name=category_name))
        
        # Track which year-month directories had folders deleted
        affected_year_months = set()
        
        # Iterate through each date and delete day-level folders
        for date in dates_to_purge:
            year_month = date.strftime("%Y-%m")
            day_path = os.path.join(ultimate_root, year_month, f"{date.day:02d}-{date.strftime('%a')[:3]}")
            
            if category_name is not None:
                # If category_name is specified, only delete that category
                category_path = os.path.join(day_path, category_name)
                if os.path.exists(category_path):
                    import shutil
                    shutil.rmtree(category_path)
                    affected_year_months.add(year_month)
                    
                    # If day folder is now empty, remove it too
                    if os.path.exists(day_path) and not os.listdir(day_path):
                        shutil.rmtree(day_path)
            else:
                # If no category_name specified, delete the entire day folder
                if os.path.exists(day_path):
                    import shutil
                    shutil.rmtree(day_path)
                    affected_year_months.add(year_month)
                
        # Check if any affected year-month directories are now empty
        for year_month in affected_year_months:
            year_month_path = os.path.join(ultimate_root, year_month)
            if os.path.isdir(year_month_path) and not os.listdir(year_month_path):
                os.rmdir(year_month_path)

    @classmethod
    def active_dates(cls, begin_date:Optional[datetime.date], ultimate_root:str,
                    end_date:Optional[datetime.date]=None, 
                    category_name:Optional[str]=None) -> Generator[datetime.date, None, None]:
        """
        Returns a generator of all dates that have folder instances of the given type within the given dates (inclusive).
        
        Args:
            begin_date: Start date for search. If None, will use first day of earliest YYYY-MM directory found
            ultimate_root: Root directory to search in
            end_date: End date for search. If None, defaults to today
            category_name: Optional category name to filter by
        """
        if begin_date is None:
            # Get a temporary path to find the root directory
            temp_path_parts = cls._tentative_new_folder_path(category_name=category_name or "UNUSEDZZZ", 
                                                           ultimate_root=ultimate_root,
                                                           dte=datetime.date.today(), 
                                                           uniqueness_src="UNUSEDZZZ")
            root_dir = temp_path_parts[0]
            
            # Find all YYYY-MM directories and get the earliest one
            try:
                month_dirs = [d for d in os.listdir(root_dir) if re.match(r"\d{4}-\d{2}", d)]
                if not month_dirs:
                    return  # No directories found, return without yielding anything
                earliest_month = min(month_dirs)
                # Set begin_date to first day of earliest month found
                begin_date = datetime.date.fromisoformat(f"{earliest_month}-01")
            except FileNotFoundError:
                return  # Root directory doesn't exist, return without yielding anything

        path_parts = cls._tentative_new_folder_path(category_name=category_name or "UNUSEDZZZ", 
                                                   ultimate_root=ultimate_root, 
                                                   dte=begin_date, 
                                                   uniqueness_src="UNUSEDZZZ")
        end_date = end_date or datetime.date.today()
        begin_m, end_m = begin_date.strftime("%Y-%m"), end_date.strftime("%Y-%m")
        s_begin, s_end = begin_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")
        
        # Track dates we've already yielded to avoid duplicates
        yielded_dates = set()
        
        # make a list of all month folders for all between s_begin and s_end
        try:
            month_folders = [f for f in os.listdir(path_parts[0]) if re.match(r"\d{4}-\d{2}", f) and begin_m <= f <= end_m]
            
            for month in month_folders:
                month_path = os.path.join(path_parts[0], month)
                try:
                    day_chunks = os.listdir(month_path)
                    
                    for day_chunk in day_chunks:
                        match = re.match(r"\d{2}-\w{3}", day_chunk)
                        if match and s_begin <= f"{month}-{day_chunk[:2]}" <= s_end:  # Use just the DD part
                            day_path = os.path.join(month_path, day_chunk)
                            
                            # If category_name is specified, only yield if that category exists for this date
                            if category_name is not None:
                                category_path = os.path.join(day_path, category_name)
                                if not os.path.exists(category_path):
                                    continue
                                    
                            # Create date from YYYY-MM and DD components
                            current_date = datetime.date.fromisoformat(f"{month}-{day_chunk[:2]}")
                            
                            # Only yield if we haven't seen this date before
                            if current_date not in yielded_dates:
                                yielded_dates.add(current_date)
                                yield current_date
                except FileNotFoundError:
                    continue
        except FileNotFoundError:
            return  # Root directory doesn't exist, return without yielding anything

    @classmethod
    def default_ultimate_root(cls, new_ultimate_root:str=None) -> str:
        """
        Returns the ultimate pipeline root folder path.  
        If new_ultimate_root is provided, it sets the ultimate pipeline root folder path to that value.
        """
        if new_ultimate_root is not None:
            cls._default_ultimate_pipeline_root = os.path.abspath(new_ultimate_root)
        if cls._default_ultimate_pipeline_root is None:
            raise Exception("ultimate_root() must be called before any PipelineFolder instances are created")
        return cls._default_ultimate_pipeline_root

    @classmethod
    def hms_str(cls, dte:datetime.datetime = datetime.datetime.now()) -> str:
        """Returns a string of the form HHMMSS_MS, representing the number of hours, minutes, seconds, and milliseconds since midnight."""
        return dte.strftime("%H%M%S_%f")[:-3]

    @classmethod
    def _tentative_new_folder_path(cls, ultimate_root:str, category_name:str, 
                                  dte:Union[datetime.date,str]=datetime.date.today(), 
                                  uniqueness_src:Optional[str]=None) -> List[str]:
        """Constructs the deterministic parts of a folder to be created for a new instance.
        May not be unique enough if many instances are created in a short period of time.

        Note: Removing the last element will provide a path that lists all directories for today with the given category name.

        The last component is the uniqueness source. This is a string that is used to make the folder name unique.
        If uniqueness_src is not provided, it is generated based on the current date and time.

        Note that the exact algorithm used here should be an internal implementation detail and not exposed to the user.
        The structure should be easy to browse in a date-centric manner.

        IMPLEMENTATION NOTE: While the algorithm is subject to change, several other functions rely upon it.
        This method does not actually check that the given directory exists.
        """
        
        # The uniqueness source is a string that is used to make the folder name unique for each individual pipeline instance.
        if uniqueness_src is None:
            uniqueness_src = cls.hms_str(dte)
        else:  # if the user passed as an acceptable uniqueness source, then we can use it directly
            try:
                uniqueness_src = str(uniqueness_src) # anything convertible to string is fine
            except:
                raise ValueError(f"uniqueness_src must be a string or datetime.date, got {type(uniqueness_src)}")
        
        # Handle the special case where dte is a string (used by active_dates)
        if isinstance(dte, str):
            # Just return the root path since this is only used for directory scanning
            return [ultimate_root]
        
        day_str = f"{dte.day:02d}-{dte.strftime('%a')[:3]}"
        return [ultimate_root, dte.strftime("%Y-%m"), day_str, category_name, uniqueness_src]

    @classmethod
    def _mod_to_unique_dir(cls, base_dir: str, uniqueness_src:str, initial_random_char_len:Optional[int]=None, make_dir:bool=True) -> str:
        """
        Given a proposed directory name, this function will append additional random characters to the end (if necessary)
        until the directory name is unique and not currently in existence.

        This can result in variable length folder names, but probably won't unless many instances are created in a short period of time.
        If make_dir is True, the directory will be created before return.
        """
        # Initialize new_path with the base directory
        new_path = base_dir
        
        # below is the first part appended to dir name if uniqueness is not found
        xtra_random = cls.RANDOM_CHAR_DELIM + ''.join([random.choice(_ALPHANUMERIC_CHARS) for _ in range(initial_random_char_len or cls.EXTRA_RANDOM_CHARS)])
        
        while True:
            if not os.path.exists(new_path):
                if make_dir:
                    os.makedirs(new_path, exist_ok=False)
                return new_path
            new_path = f"{new_path}{xtra_random}"
            xtra_random = random.choice(_ALPHANUMERIC_CHARS) # will be added if new_path not unique

    @classmethod
    def delete_folders(cls, ultimate_root:str, year:int, month:Optional[int] = None, day:Optional[int] = None) -> None:
        """
        Delete folders matching the specified date criteria. If month is not specified, deletes all folders for the year.
        If day is not specified but month is, deletes all folders for that month.
        Does nothing if no matching folders exist.
        
        Args:
            ultimate_root: The root directory containing the date tree
            year: The year to delete folders from
            month: Optional month (1-12) to restrict deletion to
            day: Optional day of month (1-31) to restrict deletion to. Month must be specified if day is.
        
        Raises:
            ValueError: If day is specified without month, or if date parameters are invalid
        """
        if day is not None and month is None:
            raise ValueError("Month must be specified if day is specified")
            
        # Validate date parameters
        if month is not None and not (1 <= month <= 12):
            raise ValueError(f"Month must be between 1 and 12, got {month}")
        if day is not None and not (1 <= day <= 31):
            raise ValueError(f"Day must be between 1 and 31, got {day}")
            
        # Format the year-month pattern we're looking for
        if month is None:
            year_month_pattern = f"{year:04d}-"
        else:
            year_month_pattern = f"{year:04d}-{month:02d}"
            
        try:
            # Find all matching year-month folders
            for folder in os.listdir(ultimate_root):
                if not folder.startswith(year_month_pattern):
                    continue
                    
                month_path = os.path.join(ultimate_root, folder)
                if not os.path.isdir(month_path):
                    continue
                    
                if day is None:
                    # Delete entire month folder
                    import shutil
                    shutil.rmtree(month_path)
                else:
                    # Delete specific day folders
                    day_pattern = f"{day:02d}-"
                    for day_folder in os.listdir(month_path):
                        if day_folder.startswith(day_pattern):
                            day_path = os.path.join(month_path, day_folder)
                            if os.path.isdir(day_path):
                                import shutil
                                shutil.rmtree(day_path)
                                
                    # Clean up empty month folders
                    if not os.listdir(month_path):
                        os.rmdir(month_path)
                        
        except FileNotFoundError:
            # If the root or any intermediate directory doesn't exist, just return
            return

    def __repr__(self) -> str:
        """Returns a string representation showing the folder path."""
        return f"DateTreeFolder(path='{str(self.path)}')"

@click.command()
@click.argument('category_name')
@click.argument('root_dir')
@click.option('--uniqueness-src', '-u', help='Optional uniqueness source for the folder name')
@click.option('--datetime', '-d', 'datetime_str', help='Optional ISO format datetime (YYYY-MM-DD[THH:MM:SS])')
def create_folder(category_name: str, root_dir: str, uniqueness_src: str = None, datetime_str: str = None) -> str:
    """
    Create a new DateTreeFolder with the given parameters.
    
    CATEGORY_NAME: Name of the category folder to create
    ROOT_DIR: Root directory where the date tree structure will be created
    """
    # Parse datetime if provided
    if datetime_str:
        try:
            # Try parsing with time component
            try:
                dte = datetime.datetime.fromisoformat(datetime_str)
            except ValueError:
                # If no time component, append current time
                current_time = datetime.datetime.now().strftime("T%H:%M:%S")
                dte = datetime.datetime.fromisoformat(f"{datetime_str}{current_time}")
        except ValueError:
            raise click.BadParameter('datetime must be in ISO format (YYYY-MM-DD[THH:MM:SS])')
    else:
        dte = datetime.datetime.now()

    # Create the folder
    folder = DateTreeFolder(
        category_name=category_name,
        ultimate_root=root_dir,
        uniqueness_src=uniqueness_src,
        dte=dte.date()
    )
    
    # Return the full path
    click.echo(folder.abspath)
    return folder.abspath

if __name__ == '__main__':
    create_folder()

