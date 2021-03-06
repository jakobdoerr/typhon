"""
This module contains classes to handle datasets consisting of many files.

Created by John Mrziglod, June 2017
"""

import atexit
from collections import defaultdict, Iterable, OrderedDict
import copy
from datetime import datetime, timedelta
import gc
import glob
from itertools import tee
import json
import logging
from multiprocessing import Pool as ProcessPool
from multiprocessing.pool import ThreadPool
from queue import Queue
import os.path
import re
import shutil
import threading
from time import time
import traceback
import warnings

import numpy as np
import pandas as pd
import typhon.files
import typhon.plots
from typhon.spareice.array import GroupedArrays
from typhon.spareice.handlers import CSV, expects_file_info, FileInfo, NetCDF4
from typhon.trees import IntervalTree
from typhon.utils.time import set_time_resolution, to_datetime, to_timedelta
import xarray as xr

__all__ = [
    "Dataset",
    "DatasetManager",
    "DataSlider",
    "InhomogeneousFilesError",
    "NoFilesError",
    "NoHandlerError",
    "UnknownPlaceholderError",
    "PlaceholderRegexError",
]


class InhomogeneousFilesError(Exception):
    """Should be raised if the files of a dataset do not have the same internal
    structure but it is required.
    """
    def __init__(self, *args):
        Exception.__init__(self, *args)


class NoFilesError(Exception):
    """Should be raised if no files were found by the :meth:`find`
    method.

    """
    def __init__(self, dataset, start, end, *args):
        if start == datetime.min and end >= datetime.max-timedelta(seconds=1):
            message = f"Found no files for {dataset.name}!"
        else:
            message = f"Found no files for {dataset.name} between {start} " \
                      f"and {end}!"

        message += f"\nPath: {dataset.path}\nCheck the path for misspellings" \
                   f" and whether there are files in this time period."
        Exception.__init__(self, message, *args)


class NoHandlerError(Exception):
    """Should be raised if no file handler is specified in a dataset object but
    a handler is required.
    """
    def __init__(self, msg, *args):
        message = f"{msg} I do not know which file handler to " \
                  f"use. Set one by yourself."
        Exception.__init__(self, message, *args)


class UnfilledPlaceholderError(Exception):
    """Should be raised if a placeholder was found that cannot be filled.
    """
    def __init__(self, name, placeholder_name=None, *args):
        if placeholder_name is None:
            message = \
                "The path of '%s' contains a unfilled placeholder!" % (name,)
        else:
            message = \
                "The dataset '%s' could not fill the placeholder %s!" % (
                    name, placeholder_name)
        Exception.__init__(self, message, *args)


class UnknownPlaceholderError(Exception):
    """Should be raised if a placeholder was found that was not defined before.
    """
    def __init__(self, name, placeholder_name=None, *args):
        if placeholder_name is None:
            message = \
                "The path of '%s' contains a unknown placeholder!" % (name,)
        else:
            message = \
                "The dataset '%s' does not know the placeholder %s!" % (
                    name, placeholder_name)
        Exception.__init__(self, message, *args)


class PlaceholderRegexError(Exception):
    """Should be raised if the regex of a placeholder is broken.
    """
    def __init__(self, name, msg):
        Exception.__init__(
            self, f"The path of '{name}' contains syntax errors: {msg}"
        )


class Dataset:
    """Provide methods to handle a set of multiple files (dataset).

    For more examples and an user guide, look at this tutorial_.

    .. _tutorial: http://radiativetransfer.org/misc/typhon/doc-trunk/tutorials/dataset.html

    Examples:

        Dataset with multiple files:

        .. code-block:: python

            from typhon.spareice import Dataset

            # Define a dataset consisting of multiple files:
            dataset = Dataset(
                path="/dir/{year}/{month}/{day}/{hour}{minute}{second}.nc",
                name="TestData",
                # If the time coverage of the data cannot be retrieved from the
                # filename, you should set this to "handler" and giving a file
                # handler to this object:
                info_via="filename"
            )

            # Find some files of the dataset:
            for file in dataset.find("2017-01-01", "2017-01-02"):
                # Should print the path of the file and its time coverage:
                print(file)

        Dataset with a single file:

        .. code-block:: python

            # Define a dataset consisting of a single file:
            dataset = Dataset(
                # Simply use the path without placeholders:
                path="/path/to/file.nc",
                name="TestData2",
                # The time coverage of the data cannot be retrieved from the
                # filename (because there are no placeholders). You can use the
                # file handler get_info() method with info_via="handler" or you
                # can define the time coverage here directly:
                time_coverage=("2007-01-01 13:00:00", "2007-01-14 13:00:00")
            )

    References:
        This is inspired by the implemented dataset classes in atmlab_ written
        by Gerrit Holl.

        .. _atmlab: http://www.radiativetransfer.org/tools/

    """

    # Required temporal placeholders that can be overridden by the user but
    # not deleted:
    _time_placeholder = {
        # "placeholder_name": [regex to find the placeholder]
        "year": "\d{4}",
        "year2": "\d{2}",
        "month": "\d{2}",
        "day": "\d{2}",
        "doy": "\d{3}",
        "hour": "\d{2}",
        "minute": "\d{2}",
        "second": "\d{2}",
        "millisecond": "\d{3}",
        "end_year": "\d{4}",
        "end_year2": "\d{2}",
        "end_month": "\d{2}",
        "end_day": "\d{2}",
        "end_doy": "\d{3}",
        "end_hour": "\d{2}",
        "end_minute": "\d{2}",
        "end_second": "\d{2}",
        "end_millisecond": "\d{3}",
    }

    # Placeholders that can be changed by the user:
    _user_placeholder = {}

    _temporal_resolution = OrderedDict({
        # time placeholder: [pandas frequency, resolution rank]
        "year": timedelta(days=366),
        "month": timedelta(days=31),
        "day": timedelta(days=1),
        "hour": timedelta(hours=1),
        "minute": timedelta(minutes=1),
        "second": timedelta(seconds=1),
        "millisecond": timedelta(microseconds=1000),
    })

    # If one has a year with two-digit representation, all years equal or
    # higher than this threshold are based onto 1900, all years below are based
    # onto 2000.
    year2_threshold = 65

    # Default handler
    default_handler = {
        "nc": NetCDF4(),
        "h5": NetCDF4(),
        "txt": CSV(),
        "csv": CSV(),
        "asc": CSV(),
    }

    # Special characters that show whether a path contains a regex or
    # placeholder:
    _special_chars = ["{", "*", "[", "\\", "<", "(", "?", "!", "|"]

    def __init__(
            self, path, handler=None, name=None, info_via=None,
            time_coverage=None, info_cache=None, exclude=None,
            placeholder=None, max_threads=None, max_processes=None,
            worker_type=None, read_args=None, write_args=None,
            concat_args=None, merge_args=None, compress=True, decompress=True,
    ):
        """Initializes a dataset object.

        Args:
            path: A string with the complete path to the dataset files. The
                string can contain placeholder such as {year}, {month},
                etc. See below for a complete list. The direct use of
                restricted regular expressions is also possible. Please note
                that instead of dots '.' the asterisk '\*' is interpreted as
                wildcard. If no placeholders are given, the path must point to
                a file. This dataset is then seen as a single file dataset.
                You can also define your own placeholders by using the
                parameter *placeholder*.
            name: The name of the dataset.
            handler: An object which can handle the dataset files.
                This dataset class does not care which format its files have
                when this file handler object is given. You can use a file
                handler class from typhon.handlers, use
                :class:`~typhon.spareice.handlers.FileHandler` or write your
                own class. If no file handler is given, an adequate one is
                automatically selected for the most common filename suffixes.
                Please note that if no file handler is specified (and none
                could set automatically), this dataset's functionality is
                restricted.
            info_via: Defines how further information about the file will
                be retrieved (e.g. time coverage). Possible options are
                *filename*, *handler* or *both*. Default is *filename*. That
                means that the placeholders in the file's path will be parsed
                to obtain information. If this is *handler*, the
                :meth:`~typhon.spareice.handlers.FileInfo.get_info` method is
                used. If this is *both*, both options will be executed but the
                information from the file handler overwrites conflicting
                information from the filename.
            info_cache: Retrieving further information (such as time coverage)
                about a file may take a while, especially when *get_info* is
                set to *handler*. Therefore, if the file information is cached,
                multiple calls of :meth:`find` (for time periods that
                are close) are significantly faster. Specify a name to a file
                here (which need not exist) if you wish to save the information
                data to a file. When restarting your script, this cache is
                used.
            time_coverage: If this dataset consists of multiple files, this
                parameter is the relative time coverage (i.e. a timedelta, e.g.
                "1 hour") of each file. If the ending time of a file cannot be
                retrieved by its file handler or filename, it is then its
                starting time + *time_coverage*. Can be a timedelta object or
                a string with time information (e.g. "2 seconds"). Otherwise
                the missing ending time of each file will be set to its
                starting time. If this
                dataset consists of a single file, then this is its absolute
                time coverage. Set this to a tuple of timestamps (datetime
                objects or strings). Otherwise the period between year 1 and
                9999 will be used as a default time coverage.
            exclude: A list of time periods (tuples of two timestamps) that
                will be excluded when searching for files of this dataset.
            placeholder: A dictionary with pairs of placeholder name matching
                regular expression. These are user-defined placeholders, the
                standard temporal placeholders do not have to be defined.
            max_threads: Maximal number of threads that will be used for
                parallelising some methods (e.g. writing in background). This
                sets also the default for
                :meth:`~typhon.spareice.datasets.Dataset.map`-like methods
                (default is 4).
            max_processes: Maximal number of processes that will be used for
                parallelising some methods. This sets also the default for
                :meth:`~typhon.spareice.datasets.Dataset.map`-like methods
                (default is 8).
            worker_type: The type of the workers that will be used to
                parallelise some methods. Can be *process* (default) or
                *thread*.
            compress: If true and the *path* path ends with a compression
                suffix (such as *.zip*, *.gz*, *.b2z*, etc.), newly created
                dataset files will be compressed after writing them to disk.
                Default value is true.
            decompress: If true and the *path* path ends with a compression
                suffix (such as *.zip*, *.gz*, *.b2z*, etc.), dataset files
                will be decompressed before reading them. Default value is
                true.
            read_args: Additional keyword arguments in a dictionary that should
                be passed to :meth:`read`.
            write_args: Additional keyword arguments in a dictionary that
                should be passed to :meth:`write`.
            merge_args:
            concat_args:

        Allowed placeholders in the *path* argument are:

        +-------------+------------------------------------------+------------+
        | Placeholder | Description                              | Example    |
        +=============+==========================================+============+
        | year        | Four digits indicating the year.         | 1999       |
        +-------------+------------------------------------------+------------+
        | year2       | Two digits indicating the year. [1]_     | 58 (=2058) |
        +-------------+------------------------------------------+------------+
        | month       | Two digits indicating the month.         | 09         |
        +-------------+------------------------------------------+------------+
        | day         | Two digits indicating the day.           | 08         |
        +-------------+------------------------------------------+------------+
        | doy         | Three digits indicating the day of       | 002        |
        |             | the year.                                |            |
        +-------------+------------------------------------------+------------+
        | hour        | Two digits indicating the hour.          | 22         |
        +-------------+------------------------------------------+------------+
        | minute      | Two digits indicating the minute.        | 58         |
        +-------------+------------------------------------------+------------+
        | second      | Two digits indicating the second.        | 58         |
        +-------------+------------------------------------------+------------+
        | millisecond | Three digits indicating the millisecond. | 999        |
        +-------------+------------------------------------------+------------+
        .. [1] Numbers lower than 65 are interpreted as 20XX while numbers
            equal or greater are interpreted as 19XX (e.g. 65 = 1965,
            99 = 1999)

        All those place holders are also allowed to have the prefix *end*
        (e.g. *end_year*). They represent the end of the time coverage.
        """

        # Initialize member variables:
        self._name = None
        self.name = name

        # Flag whether this is a single file dataset (will be derived in the
        # path setter method automatically):
        self.single_file = None

        # Complete the standard time placeholders. This must be done before
        # setting the path to the dataset's files.
        self._time_placeholder = self._complete_placeholders_regex(
            self._time_placeholder
        )

        # The path parameters (will be set and documented in the path setter
        # method):
        self._path = None
        self._path_placeholders = None
        self._end_time_superior = None
        self._path_extension = None
        self._path_regex = None
        self._base_dir = None
        self._sub_dir = ""
        self._sub_dir_chunks = []
        self._sub_dir_time_resolution = None
        self.path = path

        # Add user-defined placeholders:
        if placeholder is not None:
            self.set_placeholders(**placeholder)

        if handler is None:
            # Try to derive the file handler from the files extension but
            # before we might remove potential compression suffixes:
            basename, extension = os.path.splitext(self.path)
            if typhon.files.is_compression_format(extension.lstrip(".")):
                _, extension = os.path.splitext(basename)

            extension = extension.lstrip(".")

            self.handler = self.default_handler.get(extension, None)
        else:
            self.handler = handler

        # Defines which method will be used by .get_info():
        if info_via is None or info_via == "filename":
            self.info_via = "filename"
        else:
            if self.handler is None:
                raise NoHandlerError(f"Cannot set 'info_via' to '{info_via}'!")
            else:
                self.info_via = info_via

        # A list of time periods that will be excluded when searching files:
        self._exclude = None
        self.exclude = exclude

        # The default worker settings for map-like functions
        self.max_threads = 4 if max_threads is None else max_threads
        self.max_processes = 4 if max_processes is None else max_processes
        self.worker_type = "process" if worker_type is None else worker_type

        # The default settings for read and write methods
        self.read_args = {} if read_args is None else read_args
        self.write_args = {} if write_args is None else write_args

        # Data merging and concatenating arguments:
        self.merge_args = {} if merge_args is None else merge_args
        self.concat_args = {} if concat_args is None else concat_args

        self.compress = compress
        self.decompress = decompress

        self._time_coverage = None
        self.time_coverage = time_coverage

        # Multiple calls of .find() can be very slow when using the handler as
        # as information retrieving method. Hence, we use a cache to store the
        # names and time coverages of already touched files in this dictionary.
        self.info_cache_filename = info_cache
        self.info_cache = {}
        if self.info_cache_filename is not None:
            try:
                # Load the time coverages from a file:
                self.load_info_cache(self.info_cache_filename)
            except Exception as e:
                raise e
            else:
                # Save the time coverages cache into a file before exiting.
                # This will be executed as well when the python code is
                # aborted due to an exception. This is normally okay, but what
                # happens if the error occurs during the loading of the time
                # coverages? We would overwrite the cache with nonsense.
                # Therefore, we need this code in this else block.
                atexit.register(Dataset.save_info_cache,
                                self, self.info_cache_filename)

        # Writing processes can be moved to background threads. But we do want
        # to have too many backgrounds threads running at the same time, so we
        # create FIFO queue. The queue limits the number of parallel threads
        # to a maximum. The users can also make sure that all writing threads
        # are finished before they move on in the code.
        # TODO: We cannot use queues as attributes for Dataset because they
        # TODO: cannot be pickled.
        # self._write_queue = Queue(max_threads)

        # Dictionary for holding links to other datasets:
        self._link = {}

    def __iter__(self):
        return iter(self.find())

    def __contains__(self, item):
        """Checks whether a timestamp is covered by this dataset.

        Notes:
            This only gives proper results if the dataset consists of
            continuous data (files that covers a time span instead of only one
            timestamp).

        Args:
            item: Either a string with time information or datetime object.
                Can be also a tuple or list of strings / datetime objects that
                will be checked.

        Returns:
            True if timestamp is covered.
        """
        if isinstance(item, (tuple, list)):
            if len(item) != 2:
                raise ValueError("Can only test single timestamps or time "
                                 "periods consisting of two timestamps")

            start = to_datetime(item[0])
            end = to_datetime(item[1])
        else:
            start = to_datetime(item)
            end = start + timedelta(microseconds=1)

        try:
            next(self.find(start, end, no_files_error=False, sort=False,))
            return True
        except StopIteration:
            return False

    def __getitem__(self, item):
        if isinstance(item, (tuple, list)):
            time_args = item[0]
            filters = item[1]
        else:
            time_args = item
            filters = None

        if isinstance(time_args, slice):
            return self.collect(
                time_args.start, time_args.stop, filters=filters,
            )
        elif isinstance(time_args, (datetime, str)):
            filename = self.find_closest(time_args, filters=filters)
            if filename is None:
                return None

            return self.read(filename)

    def __setitem__(self, key, value):
        if isinstance(key, (tuple, list)):
            time_args = key[0]
            fill = key[1]
        else:
            time_args = key
            fill = None

        if isinstance(time_args, slice):
            start = time_args.start
            end = time_args.stop
        else:
            start = end = time_args

        self.write(value, times=(start, end), fill=fill)

    def __repr__(self):
        return str(self)

    def __str__(self):
        dtype = "Single-File" if self.single_file else "Multi-File"

        info = "Name:\t" + self.name
        info += "\nType:\t" + dtype
        info += "\nFiles path:\t" + self.path
        return info

    def collect(self, start=None, end=None, files=None, read_args=None,
                return_info=False, concat=True, concat_args=None, **find_args):
        """Load all files between two dates sorted by their starting time

        This parallelizes the reading of the files by using threads. This
        should give a speed up if the file handler's read function internally
        uses CPython code that releases the GIL. Note that this method is
        faster than :meth:`icollect` but also more memory consuming.

        Use this if you need all files at once but if want to use a for-loop
        consider using :meth:`icollect` instead.

        Args:
            start: Start date either as datetime object or as string
                ("YYYY-MM-DD hh:mm:ss"). Year, month and day are required.
                Hours, minutes and seconds are optional.
            end: End date. Same format as "start".
            files: If you have already a list of files that you want to
                process, pass it here. The list can contain filenames or lists
                (bundles) of filenames. If this parameter is given, it is not
                allowed to set *start* and *end* then.
            read_args: Additional key word arguments for the
                *read* method of the used file handler class.
            return_info: If true, return a FileInfo object with each content
                value indicating to which file the function was applied.
            concat: If true (default), return the data concatenated by using
                standard concatenate functions.
            **find_args: Additional keyword arguments that are allowed
                for :meth:`find`.

        Yields:
            A list of tuples with of the FileInfo object of a file and its
            content. The list is sorted by the starting times of the files.

        Examples:

        .. code-block:: python

            data_list = dataset.collect("2018-01-01", "2018-01-02")
            # If contents are numpy arrays
            data = np.hstack(contents)

            ## If you only want to concatenate the data, use this magic method:
            data = np.hstack(dataset["2018-01-01":"2018-01-02"])

            ## If you want to iterate through the files in a for loop, e.g.:
            for content in dataset.collect("2018-01-01", "2018-01-02"):
                # do something with file and content...

            # Then you should rather use icollect, which uses less memory:
            for content in dataset.icollect("2018-01-01", "2018-01-02"):
                # do something with file and content...

        """
        if read_args is None:
            read_args = {}

        if concat_args is None:
            concat_args = {}

        # If we used map with processes, it would need to pickle the data
        # coming from all workers. This would be very inefficient. Threads
        # are better because sharing data does not cost much and a file
        # reading function is typically io-bound. However, if the reading
        # function consists mainly of pure python code that does not
        # release the GIL, this will slow down the performance.
        results = self.map(
            start, end, files, func=Dataset.read, args=(self,),
            kwargs=read_args, worker_type="thread", return_info=True,
            **find_args
        )

        # Tell the python interpreter explicitly to free up memory to improve
        # performance (see https://stackoverflow.com/q/1316767/9144990):
        gc.collect()

        # We do not want to have any None as data
        files, data = zip(*[
            [info, content]
            for info, content in results
            if content is not None
        ])

        if concat:
            data = self._concat_data(data, **concat_args)

        if return_info:
            return files, data
        else:
            return data

    def icollect(self, start=None, end=None, files=None, read_args=None,
                 preload=True, return_info=False, **find_args):
        """Load all files between two dates sorted by their starting time

        Use this in for-loops but if you need all files at once, use
        :meth:`collect` instead.

        Does the same as :meth:`collect` but works as a generator and is
        therefore less memory space consuming but also slower.

        Args:
            start: Start date either as datetime object or as string
                ("YYYY-MM-DD hh:mm:ss"). Year, month and day are required.
                Hours, minutes and seconds are optional.
            end: End date. Same format as "start".
            files: If you have already a list of files that you want to
                process, pass it here. The list can contain filenames or lists
                (bundles) of filenames. If this parameter is given, it is not
                allowed to set *start* and *end* then.
            read_args: Additional key word arguments for the
                *read* method of the used file handler class.
            preload: Per default this method loads the next file to yield in a
                background thread. Set this to False, if you do not want this.
            return_info: If true, return a FileInfo object with each return
                value indicating to which file the function was applied.
            **find_args: Additional keyword arguments that are allowed
                for :meth:`find`.

        Yields:
            A tuple of the FileInfo object of a file and its content. These
            tuples are yielded sorted by its file starting time.

        Examples:

        .. code-block:: python

            ## Perfect for iterating over many files.
            for content in dataset.icollect("2018-01-01", "2018-01-02"):
                # do something with file and content...

            ## If you want to have all files at once, do not use this:
            data_list = list(dataset.icollect("2018-01-01", "2018-01-02"))

            # This version is faster:
            data_list = dataset.collect("2018-01-01", "2018-01-02")
        """

        if read_args is None:
            read_args = {}

        if preload:
            results = self.imap(
                start, end, files, func=Dataset.read, args=(self,),
                kwargs=read_args, worker_type="thread", return_info=True,
                **find_args
            )

            for info, data in results:
                if return_info:
                    yield info, data
                else:
                    yield data
        else:
            if files is None:
                files = self.find(start, end, **find_args)

            for info in files:
                data = self.read(info, **read_args)
                if data is not None:
                    if return_info:
                        yield info, data
                    else:
                        yield data

    def copy(
            self, start=None, end=None, to=None, convert=None,
            delete_originals=False,
    ):
        """Copy files from this dataset to another location.

        Args:
            start: Start date either as datetime object or as string
                ("YYYY-MM-DD hh:mm:ss"). Year, month and day are required.
                Hours, minutes and seconds are optional.
            end: End date. Same format as "start".
            to: Either a Dataset object or the new path of the files containing
                placeholders (such as {year}, {month}, etc.).
            convert: If true, the files will be read by the old dataset's file
                handler and written to their new location by using the new file
                handler from *to*. Both file handlers must be compatible, i.e.
                the object that the old file handler's read method returns must
                handable for the new file handler's write method. You
                can also set this to a function that converts the return value
                of the read method into something else before it will be passed
                to the write method. Default is false, i.e. the file will be
                simply copied without converting.
            delete_originals: If true, then all copied original files will be
                deleted. Be careful, this cannot get undone!

        Returns:
            New Dataset object with the new files.

        Examples:

        .. code-block:: python

            ## Copy all files between two dates to another location

            old_dataset = Dataset(
                "old/path/{year}/{month}/{day}/{hour}{minute}{second}.nc",
            )

            # New dataset with other path
            new_dataset = Dataset(
                "new/path/{year}/{doy}/{hour}{minute}{second}.nc",
            )

            old_dataset.copy(
                "2017-09-15", "2017-09-23", new_dataset,
            )

        .. code-block:: python

            ## Copy all files between two dates to another location and convert
            ## them to a different format

            from typhon.spareice.handlers import CSV, NetCDF4

            old_dataset = Dataset(
                "old/path/{year}/{month}/{day}/{hour}{minute}{second}.nc",
                handler=NetCDF4()
            )
            new_dataset = Dataset(
                "new/path/{year}/{doy}/{hour}{minute}{second}.csv",
                handler=CSV()
            )

            # Note that this only works if both file handlers are compatible
            new_dataset = old_dataset.copy(
                "2017-09-15", "2017-09-23", new_dataset, convert=True
            )
        """

        # Convert the path to a Dataset object:
        if not isinstance(to, Dataset):
            destination = copy.copy(self)
            destination.path = to
        else:
            destination = to

        if convert is None:
            convert = False

        if self.single_file:
            file_info = self.get_info(self.path)

            Dataset._copy_single_file(
                file_info, self, destination, convert, delete_originals
            )
        else:
            if destination.single_file:
                raise ValueError(
                    "Cannot copy files from multi-file to single-file "
                    "dataset!")

            copy_args = {
                "dataset": self,
                "destination": destination,
                "convert": convert,
                "delete_original": delete_originals
            }

            # Copy the files
            self.map(start, end, Dataset._copy_single_file, kwargs=copy_args)

        return destination

    @staticmethod
    def _copy_single_file(
            file_info, dataset, destination, convert, delete_original):
        """This is a small wrapper function for copying files. It is better to
        use :meth:`Dataset.copy` directly.

        Args:
            dataset:
            file_info: FileInfo object of the file that should be to copied.
            destination:
            convert:
            delete_original:

        Returns:
            None
        """

        # Generate the new file name
        new_filename = destination.generate_filename(
            file_info.times, fill=file_info.attr)

        # Shall we simply copy or even convert the files?
        if convert:
            # Read the file with the current file handler
            data = dataset.read(file_info)

            # Maybe the user has given us a converting function?
            if callable(convert):
                data = convert(data)

            # Store the data of the file with the new file handler
            destination.write(data, new_filename)

            if delete_original:
                os.remove(file_info.path)
        else:
            # Create the new directory if necessary.
            os.makedirs(os.path.dirname(new_filename), exist_ok=True)

            if delete_original:
                shutil.move(file_info.path, new_filename)
            else:
                shutil.copy(file_info.path, new_filename)

    @property
    def exclude(self):
        """Gets or sets time periods that will be excluded when searching for
        files.

        Returns:
            A IntervalTree object.
        """
        return self._exclude

    @exclude.setter
    def exclude(self, value):
        if value is None:
            self._exclude = None
        else:
            if isinstance(value, np.ndarray):
                self._exclude = IntervalTree(value)
            else:
                self._exclude = IntervalTree(np.array(value))

    def find_closest(self, timestamp, filters=None):
        """Finds either the file that covers a timestamp or is the closest to
        it.

        This method ignores the value of *Dataset.exclude*.

        Args:
            timestamp: date either as datetime object or as string
                ("YYYY-MM-DD hh:mm:ss"). Year, month and day are required.
                Hours, minutes and seconds are optional.
            filters: The same filter argument that is allowed for
                :meth:`find`.

        Returns:
            The FileInfo object of the found file. If no file was found, a
            NoFilesError is raised.
        """

        # Special case: the whole dataset consists of one file only.
        if self.single_file:
            if os.path.isfile(self.path):
                # We do not have to check the time coverage since there this is
                # automatically the closest file to the timestamp.
                return self.path
            else:
                raise ValueError(
                    "The path parameter of '%s' does not contain placeholders"
                    " and is not a path to an existing file!" % self.name)

        timestamp = to_datetime(timestamp)

        # We might need some more fillings than given by the user therefore
        # we need the error catching:
        try:
            # Maybe there is a file with exact this timestamp?
            path = self.generate_filename(timestamp,)
            if os.path.isfile(path):
                return self.get_info(path)
        except (UnknownPlaceholderError, UnfilledPlaceholderError):
            pass

        # We need to find all files that are around the given timestamp. Hence,
        # we use the sub directory time resolution to specify a time period
        # within the file should possibly be:
        if self._sub_dir_time_resolution is None:
            start = datetime.min
            end = datetime.max
        else:
            start = timestamp - self._sub_dir_time_resolution
            end = timestamp + self._sub_dir_time_resolution

        files = list(self.find(start, end, sort=False, filters=filters))

        if not files:
            return None

        times = [file.times for file in files]

        # Either we find a file that covers the certain timestamp:
        for index, time_coverage in enumerate(times):
            if IntervalTree.interval_contains(time_coverage, timestamp):
                return files[index]

        # Or we find the closest file.
        intervals = np.min(np.abs(np.asarray(times) - timestamp), axis=1)
        return files[np.argmin(intervals)]

    def find(
            self, start=None, end=None, sort=True, bundle=None, filters=None,
            no_files_error=True, verbose=False,
    ):
        """ Find all files of this dataset in a given time period.

        The *start* and *end* parameters build a semi-open interval: only the
        files that are equal or newer than *start* and older than *end* are
        going to be found.

        While searching this method checks whether the file lies in the time
        periods given by *Dataset.exclude*.

        Args:
            start: Start date either as datetime object or as string
                ("YYYY-MM-DD hh:mm:ss"). Year, month and day are required.
                Hours, minutes and seconds are optional. If not given, it is
                datetime.min per default.
            end: End date. Same format as "start". If not given, it is
                datetime.max per default.
            sort: If true, all files will be yielded
                sorted by their starting time. Default is true.
            bundle: Instead of only yielding one file at a time, you can get a
                bundle of files. There are two possibilities: by setting this
                to an integer, you can define the size of the bundle directly
                or by setting this to a string (e.g. *1H*),
                you can define the time period of one bundle. See
                http://pandas.pydata.org/pandas-docs/stable/timeseries.html#offset-aliases
                for allowed time specifications. Default value is 1. This
                argument will be ignored when having a single-file dataset.
                When using *bundle*, the returned files will always be sorted
                ignoring the state of the *sort* argument.
            filters: Limits user-defined placeholder to certain values.
                Must be a dictionary where the keys are the names of
                user-defined placeholders and the values either strings or
                lists of strings with allowed placeholder values (can be
                represented by regular expressions). If the key name starts
                with a *!* (exclamation mark), the value represent a black
                list (values that are not allowed).
            no_files_error: If true, raises an NoFilesError when no
                files are found.
            verbose: If true, debug messages will be printed.

        Yields:
            Either a :class:`~typhon.spareice.handlers.FileInfo` object for
            each found file or - if *bundle_size* is not None - a list of
            :class:`~typhon.spareice.handlers.FileInfo` objects.

        Examples:

        .. code-block:: python

            # Define a dataset consisting of multiple files:
            dataset = Dataset(
                "/dir/{year}/{month}/{day}/{hour}{minute}{second}.nc"
            )

            # Find some files of the dataset:
            for file in dataset.find("2017-01-01", "2017-01-02"):
                # file is a FileInfo object that has the attribute path
                # and times.
                print(file.path)  # e.g. "/dir/2017/01/01/120000.nc"
                print(file.times)  # list of two datetime objects
        """

        # The user can give strings instead of datetime objects:
        start = datetime.min if start is None else to_datetime(start)
        end = datetime.max if end is None else to_datetime(end)

        # We want to have a semi-open interval as explained in the doc string.
        end -= timedelta(microseconds=1)

        if end < start:
            raise ValueError(
                "The start must be smaller than the end parameter!")

        if verbose:
            print("Find files between %s and %s!" % (start, end))

        # Special case: the whole dataset consists of one file only.
        if self.single_file:
            if os.path.isfile(self.path):
                file_info = self.get_info(self.path)
                if IntervalTree.interval_overlaps(
                        file_info.times, (start, end)):
                    yield file_info
                elif no_files_error:
                    raise NoFilesError(self, start, end)
                return
            else:
                raise ValueError(
                    "The path of '%s' neither contains placeholders"
                    " nor is a path to an existing file!" % self.name)

        # Files may exceed the time coverage of their directories. For example,
        # a file located in the directory of 2018-01-13 contains data from
        # 2018-01-13 18:00:00 to 2018-01-14 02:00:00. In order to find them, we
        # must include the previous sub directory into the search range:
        if self._sub_dir_time_resolution is None or start == datetime.min:
            dir_start = start
        else:
            dir_start = start - self._sub_dir_time_resolution

        # Filter handling:
        if filters is None:
            # We can apply the standard path regex:
            regex = self._path_regex
            white_list = {}
            black_list = {}
        else:
            # Complete the regexes of the filters (simply adding curls around
            # them):
            white_list = self._complete_placeholders_regex(
                {f: v for f, v in filters.items() if not f.startswith("!")}
            )

            # The new regex for all files:
            regex = self._fill_placeholders_with_regexes(
                self.path,
                extra_placeholder=white_list
            )

            def convert(value):
                if value is None:
                    return None
                elif isinstance(value, (tuple, list)):
                    return re.compile(f"{'|'.join(value)}")
                else:
                    return re.compile(f"{value}")

            black_list = {
                f.lstrip("!"): convert(v)
                for f, v in filters.items()
                if f.startswith("!")
            }

        if verbose and filters is not None:
            print(f"Loaded filters:\nWhitelist: {white_list}"
                  f"\nBlacklist: {black_list}")

        # Find all files by iterating over all searching paths and check
        # whether they match the path regex and the time period.
        file_finder = (
            file_info
            for path, _ in self._get_search_dirs(dir_start, end, white_list)
            for file_info in self._get_matching_files(path, regex, start, end,)
            if not black_list or self._check_file(black_list, file_info.attr)
        )

        # Even if no files were found, the user does not want to know.
        if not no_files_error:
            yield from self._prepare_find_files_return(
                file_finder, sort, bundle)
            return

        # The users wants an error to be raised if no files were found. Since
        # the file_finder is an iterator, we have to check whether it is empty.
        # I do not know whether there is a more pythonic way but Matthew
        # Flaschen shows how to do it with itertools.tee:
        # https://stackoverflow.com/a/3114423
        return_files, check_files = tee(file_finder)
        try:
            next(check_files)

            # We have found some files and can return them
            yield from self._prepare_find_files_return(
                return_files, sort, bundle)
        except StopIteration as err:
            raise NoFilesError(self, start, end)

    def _get_search_dirs(self, start, end, white_list):
        """Yields all searching directories for a time period.

        Args:
            start: Datetime that defines the start of a time interval.
            end: Datetime that defines the end of a time interval. The time
                coverage of the files should overlap with this interval.
            white_list: A dictionary that limits placeholders to certain
                values.

        Returns:
            A tuple of path as string and parsed placeholders as dictionary.
        """

        # Goal: Search for all directories that match the path regex and is
        # between start and end date.
        # Strategy: Go through each folder in the hierarchy and find the ones
        # that match the regex so far. Filter out folders that does not overlap
        # with the given time interval.
        search_dirs = [(self._base_dir, {}), ]

        # If the directory does not contain regex or placeholders, we simply
        # return the base directory
        if not self._sub_dir:
            return search_dirs

        for subdir_chunk in self._sub_dir_chunks:
            # Sometimes there is a sub directory part that has no
            # regex/placeholders:
            if not any(True for ch in subdir_chunk
                       if ch in self._special_chars):
                # We can add this sub directory part because it will always
                # match to our path
                search_dirs = [
                    (os.path.join(old_dir, subdir_chunk), attr)
                    for old_dir, attr in search_dirs
                ]
                continue

            # The sub directory covers a certain time coverage, we make
            # sure that it is included into the search range.
            start_check = set_time_resolution(
                start, self._get_time_resolution(subdir_chunk)[0]
            )
            end_check = set_time_resolution(
                end, self._get_time_resolution(subdir_chunk)[0]
            )

            # compile the regex for this sub directory:
            regex = self._fill_placeholders_with_regexes(
                subdir_chunk, extra_placeholder=white_list,
            )
            search_dirs = [
                (new_dir, attr)
                for search_dir in search_dirs
                for new_dir, attr in self._get_matching_dirs(search_dir, regex)
                if self._check_placeholders(attr, start_check, end_check)
            ]

        return search_dirs

    def _get_matching_dirs(self, dir_with_attrs, regex):
        base_dir, dir_attr = dir_with_attrs
        for new_dir in glob.iglob(os.path.join(base_dir + "*", "")):
            # The glob function yields full paths, but we want only to check
            # the new pattern that was added:
            basename = new_dir[len(base_dir):].rstrip(os.sep)
            try:
                new_attr = {
                    **dir_attr,
                    **self.parse_filename(basename, regex)
                }
                yield new_dir, new_attr
            except ValueError:
                pass

    def _check_placeholders(self, attr, start, end):
        attr_start, attr_end = self._to_datetime_args(attr)
        attr_end = {**attr_start, **attr_end}
        year = attr_start.get("year", None)
        if year is not None:
            try:
                return datetime(**attr_start) >= start \
                    and datetime(**attr_end) <= end
            except:
                return year >= start.year and attr_end["year"] <= end.year

        return True

    def _get_matching_files(self, path, regex, start, end,):
        """Yield files that matches the search conditions.

        Args:
            path: Path to the directory that contains the files that should be
                checked.
            regex: A regular expression that should match the filename.
            start: Datetime that defines the start of a time interval.
            end: Datetime that defines the end of a time interval. The time
                coverage of the file should overlap with this interval.

        Yields:
            A FileInfo object with the file path and time coverage
        """

        for filename in glob.iglob(os.path.join(path, "*")):
            if regex.match(filename):
                file_info = self.get_info(filename)

                # Test whether the file is overlapping the interval between
                # start and end date.
                if IntervalTree.interval_overlaps(
                        file_info.times, (start, end))\
                        and not self.is_excluded(file_info.times):
                    yield file_info

    @staticmethod
    def _check_file(black_list, placeholders):
        """Check whether placeholders are filled with something forbidden

        Args:
            black_list: A dictionary with placeholder name and content that
                should be filtered out.
            placeholders: A dictionary with placeholders and their fillings.

        Returns:
            False if the placeholders are filled with something that is
            forbidden due to the filters. True otherwise.
        """
        for placeholder, forbidden in black_list.items():
            value = placeholders.get(placeholder, None)
            if value is None:
                continue

            if forbidden.match(value):
                return False

        return True

    @staticmethod
    def _prepare_find_files_return(file_iterator, sort, bundle_size):
        """Prepares the return value of the find method.

        Args:
            file_iterator: Generator function that yields the found files.
            sort: If true, all found files will be sorted according to their
                starting times.
            bundle_size: See the documentation of the *bundle* argument in
                :meth`find` method.

        Yields:
            Either one FileInfo object or - if bundle_size is set - a list of
            FileInfo objects.
        """
        # We want to have sorted files if we want to bundle them.
        if sort or isinstance(bundle_size, int):
            file_iterator = sorted(file_iterator, key=lambda x: x.times[0])

        if bundle_size is None:
            yield from file_iterator
            return

        # The argument bundle was defined. Either it sets the bundle size
        # directly via a number or indirectly by setting time periods.
        if isinstance(bundle_size, int):
            files = list(file_iterator)

            yield from (
                files[i:i + bundle_size]
                for i in range(0, len(files), bundle_size)
            )
        elif isinstance(bundle_size, str):
            files = list(file_iterator)

            # We want to split the files into hourly (or daily, etc.) bundles.
            # pandas provides a practical grouping function.
            time_series = pd.Series(
                files,
                [file.times[0] for file in files]
            )
            yield from (
                bundle[1].values.tolist()
                for bundle in time_series.groupby(
                pd.Grouper(freq=bundle_size))
                if bundle[1].any()
            )
        else:
            raise ValueError(
                "The parameter bundle must be a integer or string!")

    def generate_filename(
            self, times, template=None, fill=None):
        """ Generate the full path and name of a file for a time period.

        Use :meth:`parse_filename` if you want retrieve information from the
        filename instead.

        Args:
            times: Either a tuple of two datetime objects representing start
                and end time or simply one datetime object (for discrete
                files).
            template: A string with format placeholders such as {year} or
                {day}. If not given, the template in *Dataset.path* is used.
            fill: A dictionary with fillings for user-defined placeholder.

        Returns:
            A string containing the full path and name of the file.

        Example:

        .. code-block:: python

            dataset.generate_filename(
                datetime(2016, 1, 1),
                "{year2}/{month}/{day}.dat",
            )
            # Returns "16/01/01.dat"

            dataset.generate_filename(
                ("2016-01-01", "2016-12-31"),
                "{year}{month}{day}-{end_year}{end_month}{end_day}.dat",
            )
            # Returns "20160101-20161231.dat"

        """

        if isinstance(times, (tuple, list)):
            start_time = to_datetime(times[0])
            end_time = to_datetime(times[1])
        else:
            start_time = to_datetime(times)
            end_time = start_time

        if template is None:
            template = self.path

        # Remove the automatic regex completion from the user placeholders and
        # use them as default fillings
        default_fill = {
            p: self._remove_group_capturing(p, v)
            for p, v in self._user_placeholder.items()
        }
        if fill is None:
            fill = default_fill
        else:
            fill = {**default_fill, **fill}

        try:
            # Fill all placeholders variables with values
            filename = template.format(
                year=start_time.year, year2=str(start_time.year)[-2:],
                month="{:02d}".format(start_time.month),
                day="{:02d}".format(start_time.day),
                doy="{:03d}".format(
                    (start_time - datetime(start_time.year, 1, 1)).days
                    + 1),
                hour="{:02d}".format(start_time.hour),
                minute="{:02d}".format(start_time.minute),
                second="{:02d}".format(start_time.second),
                millisecond="{:03d}".format(
                    int(start_time.microsecond / 1000)),
                end_year=end_time.year, end_year2=str(end_time.year)[-2:],
                end_month="{:02d}".format(end_time.month),
                end_day="{:02d}".format(end_time.day),
                end_doy="{:03d}".format(
                    (end_time - datetime(end_time.year, 1, 1)).days
                    + 1),
                end_hour="{:02d}".format(end_time.hour),
                end_minute="{:02d}".format(end_time.minute),
                end_second="{:02d}".format(end_time.second),
                end_millisecond="{:03d}".format(
                    int(end_time.microsecond/1000)),
                **fill,
            )

            # Some placeholders might be unfilled:
            if any((c in self._special_chars) for c in filename):
                raise UnfilledPlaceholderError(self.name, filename)

            return filename

        except KeyError:
            raise UnknownPlaceholderError(self.name)

    @expects_file_info()
    def get_info(self, file_info, retrieve_via=None):
        """Get information about a file.

        How the information will be retrieved is defined by

        Args:
            file_info: A string, path-alike object or a
                :class:`~typhon.spareice.handlers.common.FileInfo` object.
            retrieve_via: Defines how further information about the file will
                be retrieved (e.g. time coverage). Possible options are
                *filename*, *handler* or *both*. Default is the value of the
                *info_via* parameter during initialization of this Dataset
                 object. If this is *filename*, the placeholders in the file's
                path will be parsed to obtain information. If this is
                *handler*, the
                :meth:`~typhon.spareice.handlers.FileInfo.get_info` method is
                used. If this is *both*, both options will be executed but the
                information from the file handler overwrites conflicting
                information from the filename.

        Returns:
            A :meth`~typhon.spareice.handlers.FileInfo` object.
        """
        # We want to save time in this routine, therefore we first check
        # whether we cached this file already.

        if file_info.path in self.info_cache:
            return self.info_cache[file_info.path]

        # We have not processed this file before.

        info = file_info.copy()
        if self.single_file:
            info.times = self.time_coverage

        if retrieve_via is None:
            retrieve_via = self.info_via

        # Parsing the filename
        if retrieve_via in ("filename", "both"):
            filled_placeholder = self.parse_filename(info.path)

            filename_info = FileInfo(
                info.path, self._retrieve_time_coverage(filled_placeholder),
                # Filter out all placeholder that are not coming from the user
                {k: v for k, v in filled_placeholder.items()
                 if k in self._user_placeholder}
            )
            info.update(filename_info)

        # Using the handler for getting more information
        if retrieve_via in ("handler", "both"):
            with typhon.files.decompress(info.path) as decompressed_path:
                decompressed_file = info.copy()
                decompressed_file.path = decompressed_path
                handler_info = self.handler.get_info(decompressed_file)
                info.update(handler_info)

        if info.times[0] is None:
            if info.times[1] is None:
                # This is obviously a non-temporal dataset, set the times to
                # minimum and maximum so we have no problem to find it
                info.times = [datetime.min, datetime.max]
            else:
                # Something went wrong, we need a starting time if we have an
                # ending time.
                raise ValueError(
                    "Could not retrieve the starting time information from "
                    "the file '%s' from the %s dataset!"
                    % (info.path, self.name)
                )
        elif info.times[1] is None:
            # Sometimes the files have only a starting time. But if the user
            # has defined a timedelta for the coverage, the ending time can be
            # calculated from this. Otherwise this is a Dataset that has only
            # files that are discrete in time
            if isinstance(self.time_coverage, timedelta):
                info.times[1] = info.times[0] + self.time_coverage
            else:
                info.times[1] = info.times[0]

        self.info_cache[info.path] = info
        return info

    def _concat_data(self, objects, **kwargs):

        if self.handler.data_merger is not None:
            func = self.handler.data_merger
        elif isinstance(objects[0], GroupedArrays):
            func = type(objects[0]).concat
        elif isinstance(objects[0], (xr.Dataset, xr.DataArray)):
            func = xr.concat
        elif isinstance(objects[0], pd.DataFrame):
            func = pd.concat
        else:
            raise ValueError(
                f"No concatenating function is specified for "
                f"{type(objects[0])}! You set one via your file handler's "
                f"data_concatenator parameter."
            )

        return func(objects, **{**self.concat_args, **kwargs})

    def _merge_data(self, objects):

        if self.handler.data_merger is not None:
            func = self.handler.data_merger
        elif isinstance(objects[0], GroupedArrays):
            func = type(objects[0]).merge
        elif isinstance(objects[0], (xr.Dataset, xr.DataArray)):
            func = xr.merge
        elif isinstance(objects[0], pd.DataFrame):
            func = pd.merge
        else:
            raise ValueError(
                f"No merging function is specified for "
                f"{type(objects[0])}! You set one via your file handler's "
                f"data_merger parameter."
            )

        return func(objects, **self.merge_args)

    def is_excluded(self, period):
        """Checks whether a time interval is excluded from this Dataset.

        Args:
            period: A tuple of two datetime objects.

        Returns:
            True or False
        """
        if self.exclude is None:
            return False

        return period in self.exclude

    def dislink(self, name_or_dataset):
        """Remove the link between this and another dataset

        Args:
            name_or_dataset: Name of a dataset or the Dataset object itself. It
                must be linked to this dataset. Otherwise a KeyError will be
                raised.

        Returns:
            None
        """
        if isinstance(name_or_dataset, Dataset):
            del self._link[name_or_dataset.name]
        else:
            del self._link[name_or_dataset]

    def link(self, other_dataset, linker=None):
        """Link this dataset with another

        If one file is read from this dataset, its corresponding file from
        `other_dataset` will be read, too. Their content will then be merged by
        using the file handler's data merging function. If it is not
        implemented, it tries to derive a standard merging function from known
        data types.

        Args:
            other_dataset: Other Dataset-like object.
            linker: Reference to a function that searches for the corresponding
                file in *other_dataset* for a given file from this dataset.
                Must accept *other_dataset* as first and a
                :class:`~typhon.spareice.handlers.common.FileInfo` object as
                parameters. It must return a FileInfo of the corresponding
                file. If none is given,
                :meth:`~typhon.spareice.datasets.Dataset.generate_filename`
                will be used as default.

        Returns:
            None
        """

        self._link[other_dataset.name] = {
            "target": other_dataset,
            "linker": linker,
        }

    def load_info_cache(self, filename):
        """ Loads the information cache from a file.

        Returns:
            None
        """
        if filename is not None and os.path.exists(filename):
            try:
                with open(filename) as file:
                    json_info_cache = json.load(file)
                    # Create FileInfo objects from json dictionaries:
                    info_cache = {
                        json_dict["path"]: FileInfo.from_json_dict(json_dict)
                        for json_dict in json_info_cache
                    }
                    self.info_cache.update(info_cache)
            except Exception as err:
                warnings.warn(
                    f"Could not load the file information from cache file "
                    "'{filename}':\n{err}."
                )

    def map(
            self, start=None, end=None, files=None, func=None, args=None,
            kwargs=None, file_arg_keys=None, on_content=False, read_args=None,
            output=None, max_workers=None, worker_type=None,
            worker_initializer=None, worker_initargs=None, return_info=False,
            **find_args
    ):
        """Apply a function on all files of this dataset between two dates.

        This method can use multiple workers processes / threads to boost the
        procedure significantly. Depending on which system you work, you should
        try different numbers for *max_workers*.

        Use this if you need to process the files as fast as possible without
        needing to retrieve the results immediately. Otherwise you should
        consider using :meth:`imap` in a for-loop.

        Notes:
            This method sorts the results after the starting time of the files
            unless *sort* is False.

        Args:
            start: Start timestamp either as datetime object or as string
                ("YYYY-MM-DD hh:mm:ss"). Year, month and day are required.
                Hours, minutes and seconds are optional.
            end: End timestamp. Same format as "start".
            files: If you have already a list of files that you want to
                process, pass it here. The list can contain filenames or lists
                (bundles) of filenames. If this parameter is given, it is not
                allowed to set *start* and *end* then.
            func: A reference to a function that should be applied.
            args: A list/tuple with positional arguments that should be passed
                to *func*. It will be extended with the file arguments, i.e.
                a FileInfo object if *on_content* is false or - if *on_content*
                is true - the read content of a file and its corresponding
                FileInfo object. If you want to pass the file arguments rather
                as key word arguments, you can use the option *file_arg_keys*.
            kwargs: A dictionary with keyword arguments that should be passed
                to *func*.
            file_arg_keys: Use this if you want to pass the file arguments as
                key word arguments. If *on_content* is false, this is the key
                name of the file info object. If *on_content* is true, this is
                a tuple of the key name of the file info object and the key
                name of the file content object.
            on_content: If true, the file will be read before *func* will be
                applied. The content will then be passed to *func*.
            read_args: Additional keyword arguments that will be passed
                to the reading function (see Dataset.read() for more
                information). Will be ignored if *on_content* is False.
            output: Set this to a path containing placeholders or a Dataset
                object and the return value of *func* will be copied there if
                it is not None.
            max_workers: Max. number of parallel workers to use. When
                lacking performance, you should change this number.
            worker_type: The type of the workers that will be used to
                parallelize *func*. Can be *process* or *thread*. If *func* is
                a function that needs to share a lot of data with its
                parallelized copies, you should set this to *thread*. Note that
                this may reduce the performance due to Python's Global
                Interpreter Lock (`GIL <https://stackoverflow.com/q/1294382>`).
            worker_initializer: Must be a reference to a function that is
                called once when initialising a new worker. Can be used to
                preload variables into a worker's workspace. See also
                https://docs.python.org/3.1/library/multiprocessing.html#module-multiprocessing.pool
                for more information.
            worker_initargs: A tuple with arguments for *worker_initializer*.
            return_info: If true, return a FileInfo object with each return
                value indicating to which file the function was applied.
            **find_args: Additional keyword arguments that are allowed
                for :meth`find`.

        Returns:
            A list with tuples of a FileInfo object and the return value of the
            function applied to this file. If *output* is set, the second
            element is not the return value but a boolean values indicating
            whether the return value was not None.

        Examples:

            .. code-block:: python

                ## Imaging you want to calculate some statistical values from the
                ## data of the files
                def calc_statistics(content, file_info):
                    # return the mean and maximum value
                    return content["data"].mean(), content["data"].max()

                results = dataset.map(
                    "2018-01-01", "2018-01-02", calc_statistics,
                    on_content=True, return_info=True,
                )

                # This will be run after processing all files...
                for file, result in results
                    print(file) # prints the FileInfo object
                    print(result) # prints the mean and maximum value

                ## If you need the results directly, you can use imap instead:
                results = dataset.imap(
                    "2018-01-01", "2018-01-02", calc_statistics,
                    on_content=True,
                )

                for result in results
                    # After the first file has been processed, this will be run
                    # immediately ...
                    print(result) # prints the mean and maximum value

            If you need to pass some args to the function, use the parameters
            *args* and *kwargs*:

            .. code-block:: python

                def calc_statistics(arg1, content, file_info, kwarg1=None):
                    # return the mean and maximum value
                    return content["data"].mean(), content["data"].max()

                results = dataset.map(
                    "2018-01-01", "2018-01-02", calc_statistics,
                    args=("value1",), kwargs={"kwarg1": "value2"},
                    on_content=True,
                )
        """

        pool, func_args_queue = self._configure_map_pool_and_worker_args(
            start, end, files, func, args, kwargs, file_arg_keys,
            on_content, read_args, output,
            max_workers, worker_type, worker_initializer, worker_initargs,
            return_info, **find_args
        )

        # Process all found files with the arguments:
        return pool.map(
            self._call_map_function, func_args_queue,
        )

    def imap(self, *args, **kwargs):
        """Apply a function on all files of this dataset between two dates.

        This method does exact the same as :meth:`map` but works as a generator
        and is therefore less memory space consuming.

        Args:
            *args: The same positional arguments as for :meth:`map`.
            **kwargs: The same keyword arguments as for :meth:`map`.

        Yields:
            A tuple with the FileInfo object of the processed file and the
            return value of the applied function. If *output* is set, the
            second element is not the return value but a boolean values
            indicating whether the return value was not None.

        """

        pool, func_args_queue = self._configure_map_pool_and_worker_args(
            *args, **kwargs
        )

        # Preload the first file
        pre_loaded = pool.apply_async(
            self._call_map_function,
            args=(next(func_args_queue), ),
        )

        for i, func_args in enumerate(func_args_queue):
            yield_this = pre_loaded
            pre_loaded = pool.apply_async(
                self._call_map_function, args=(func_args, )
            )
            yield yield_this.get()

        # Flush the last processed file
        yield pre_loaded.get()

    def _configure_map_pool_and_worker_args(
            self, start=None, end=None, files=None, func=None, args=None,
            kwargs=None, file_arg_keys=None,
            on_content=False, read_args=None, output=None,
            max_workers=None, worker_type=None, worker_initializer=None,
            worker_initargs=None, return_info=False, **find_args
    ):
        if func is None:
            raise ValueError("The parameter *func* must be given!")

        if files is not None and (start is not None or end is not None):
            raise ValueError(
                "Either *files* or *start* and *end* must be given. Not all of"
                " them!")

        # Convert the path to a Dataset object:
        if isinstance(output, str):
            output_path = output
            output = copy.copy(self)
            output.path = output_path

        if worker_type is None:
            worker_type = self.worker_type

        if max_workers is None:
            if worker_type == "thread":
                max_workers = self.max_threads
            else:
                max_workers = self.max_processes

        if worker_type == "process":
            pool = ProcessPool(
                max_workers, initializer=worker_initializer,
                initargs=worker_initargs,
            )
        elif worker_type == "thread":
            pool = ThreadPool(
                max_workers, initializer=worker_initializer,
                initargs=worker_initargs,
            )
        else:
            raise ValueError(f"Unknown worker type '{worker_type}!")

        if kwargs is None:
            kwargs = {}

        if read_args is None:
            read_args = {}

        if files is None:
            files = self.find(start, end, **find_args)

        function_arguments = (
            (self, file, func, args, kwargs, file_arg_keys, output,
             on_content, read_args, return_info)
            for file in files
        )

        return pool, function_arguments

    @staticmethod
    def _call_map_function(all_args):
        """ This is a small wrapper function to call the function that is
        called on dataset files via .map().

        Args:
            all_args: A tuple containing following elements:
                (Dataset object, file_info, function,
                args, kwargs, output, on_content, read_args, return_info)

        Returns:
            The return value of *function* called with the arguments *args* and
            *kwargs*. This arguments have been extended by file info (and file
            content).
        """
        dataset, file_info, func, args, kwargs, file_arg_keys, output, \
            on_content, read_args, return_info = all_args

        args = [] if args is None else list(args)

        timer = time()
        if on_content:
            # file_info could be a bundle of files
            if isinstance(file_info, FileInfo):
                file_content = dataset.read(file_info, **read_args)
            else:
                file_content = \
                    dataset.collect(files=file_info, read_args=read_args)
            if file_arg_keys is None:
                args.append(file_content)
            else:
                kwargs.update(**{
                    file_arg_keys[1]: file_content,
                })

        if file_arg_keys is None:
            args.append(file_info)
        else:
            kwargs.update(**{
                file_arg_keys[0]: file_info,
            })

        # Call the function:
        return_value = func(*args, **kwargs)

        def _return(file_info, return_value):
            """Small helper for return / not return the file info object."""

            if return_info:
                return file_info, return_value
            else:
                return return_value

        if output is None:
            # No output is needed, simply return the file info and the
            # function's return value
            return _return(file_info, return_value)

        if return_value is None:
            # We cannot write a file with the content None, hence simply return
            # the file info and False indicating that we did not write a file.
            return _return(file_info, False)

        # file_info could be a bundle of files
        if isinstance(file_info, FileInfo):
            new_filename = output.generate_filename(
                file_info.times, fill=file_info.attr
            )
        else:
            start_times, end_times = zip(
                *(file.times for file in file_info)
            )
            new_filename = output.generate_filename(
                (min(start_times), max(end_times)), fill=file_info[0].attr
            )

        output.write(return_value, new_filename, in_background=False)

        return _return(file_info, True)

    @property
    def name(self):
        """Gets or sets the dataset's name.

        Returns:
            A string with the dataset's name.
        """
        return self._name

    @name.setter
    def name(self, value):
        if value is None:
            value = str(id(self))

        self._name = value

    def _to_datetime_args(self, placeholder):
        """Get datetime args from placeholders for start and end date.

        Args:
            placeholder: A dictionary containing time placeholders.

        Returns:
            A tuple of two dictionaries
        """
        start_args = {
            p: int(value)
            for p, value in placeholder.items()
            if not p.startswith("end_") and p in self._time_placeholder
        }

        end_args = {
            p[len("end_"):]: int(value)
            for p, value in placeholder.items()
            if p.startswith("end_") and p in self._time_placeholder
        }

        return (
            self._standardise_datetime_args(start_args,),
            self._standardise_datetime_args(end_args,)
        )

    def _standardise_datetime_args(self, args):
        """Replace some placeholders to datetime-conform placeholder.

        Args:
            args: A dictionary of placeholders.

        Returns:
            The standardised placeholder dictionary.
        """
        year2 = args.pop("year2", None)
        if year2 is not None:
            if year2 < self.year2_threshold:
                args["year"] = 2000 + year2
            else:
                args["year"] = 1900 + year2
        millisecond = args.pop("millisecond", None)
        if millisecond is not None:
            args["microsecond"] = millisecond * 1000
        doy = args.pop("doy", None)
        if doy is not None:
            date = datetime(args["year"], 1, 1) + timedelta(doy - 1)
            args["month"] = date.month
            args["day"] = date.day

        return args

    def overlaps_with(
            self, other_dataset, start, end, max_interval=None,
            filters=None, other_filters=None):
        """Find files between two datasets that overlap in time.

        Args:
            other_dataset: A Dataset object which holds the other files.
            start: Start date either as datetime object or as string
                ("YYYY-MM-DD hh:mm:ss"). Year, month and day are required.
                Hours, minutes and seconds are optional.
            end: End date. Same format as "start".
            max_interval: Maximal time interval in seconds between
                two overlapping files. Must be an integer or float.
            filters: The same filter argument that is allowed for
                :meth:`find`.
            other_filters: The same filter argument that is allowed for
                :meth:`find`.

        Yields:
            A tuple with the names of two files which correspond to each other.
        """
        if max_interval is not None:
            max_interval = to_timedelta(max_interval)
            start = to_datetime(start) - max_interval
            end = to_datetime(end) + max_interval

        files1 = list(
            self.find(start, end, filters=filters)
        )
        files2 = list(
            other_dataset.find(start, end, filters=other_filters)
        )

        # Convert the times (datetime objects) to seconds (integer)
        times1 = [
            [int(file.times[0].timestamp()), int(file.times[1].timestamp())]
            for file in files1
        ]
        times2 = np.asarray([
            [file.times[0].timestamp(), file.times[1].timestamp()]
            for file in files2
        ]).astype('int')

        if max_interval is not None:
            # Expand the intervals of the secondary dataset to close-in-time
            # intervals.
            times2[:, 0] -= int(max_interval.total_seconds())
            times2[:, 1] += int(max_interval.total_seconds())

        tree = IntervalTree(times2)

        # Search for all overlapping intervals:
        results = tree.query(times1)

        for i, overlapping_files in enumerate(results):
            yield files1[i], [files2[oi] for oi in sorted(overlapping_files)]

    def parse_filename(self, filename, template=None,):
        """Parse the filename with temporal and additional regular expressions.

        This method uses the standard temporal placeholders which might be
        overwritten by the user-defined placeholders.

        Args:
            filename: Path and name of the file.
            template: Template with regex/placeholders that should be used.
                Default is *Dataset.path*.

        Returns:
            A dictionary with filled placeholders.
        """

        if template is None:
            regex = self._path_regex
        else:
            if isinstance(template, str):
                regex = self._fill_placeholders_with_regexes(template)
            else:
                regex = template

        results = regex.match(filename)

        if not results:
            raise ValueError(
                "Could not parse the filename; it does not match the given "
                "template.")
        else:
            return results.groupdict()

    @property
    def path(self):
        """Gets or sets the path to the dataset's files.

        Returns:
            A string with the path (can contain placeholders or wildcards.)
        """

        # We need always the absolute path:
        return os.path.abspath(self._path)

    @path.setter
    def path(self, value):
        if value is None:
            raise ValueError("The path parameter cannot be None!")

        self._path = value

        # The path consists of three parts: the base directory, the sub
        # directory and the filename. The sub directory and filename may
        # contain regex/placeholder, the base directory not. We need to split
        # the path into these three parts to enable file finding.
        directory = os.path.dirname(self.path)
        index_of_sub_directory = \
            next(
                (i for i, ch in enumerate(directory)
                 if ch in self._special_chars), None
            )

        if index_of_sub_directory is None:
            # There is no sub directory
            self._base_dir = directory
        else:
            self._base_dir = directory[:index_of_sub_directory]
            self._sub_dir = directory[index_of_sub_directory:]

            # Later, we iterate over all possible sub directories and find
            # those that match the regex / placeholders. Hence, we split the
            # sub directory into chunks for each hierarchy level:
            self._sub_dir_chunks = self._sub_dir.split(os.path.sep)

            # The sub directory time resolution is needed for find_closest:
            self._sub_dir_time_resolution = self._get_time_resolution(
                self._sub_dir
            )[1]

        # Retrieve the used placeholder names from the path:
        self._path_placeholders = set(re.findall("\{(\w+)\}", self.path))

        # Set additional user-defined placeholders to default values (
        # non-greedy wildcards).
        self.set_placeholders(**{
            p: ".+?"
            for p in self._path_placeholders.difference(self._time_placeholder)
        })

        # Get all temporal placeholders from the path (for ending time):
        end_time_placeholders = {
            p[len("end_"):] for p in self._path_placeholders
            if p.startswith("end") and p in self._time_placeholder
        }

        # If the end time retrieved from the path is younger than the start
        # time, the end time will be incremented by this value:
        self._end_time_superior = \
            self._get_superior_time_resolution(end_time_placeholders)

        # Flag whether this is a single file dataset or not. We simply check
        # whether the path contains special characters:
        self.single_file = not any(
            True for ch in self.path
            if ch in self._special_chars
        )

        self._path_extension = os.path.splitext(self.path)[0].lstrip(".")

    @staticmethod
    def _get_superior_time_resolution(placeholders, ):
        """Get the superior time resolution of all placeholders.

        Examples:
            The superior time resolution of seconds are minutes, of hours are
            days, etc.

        Args:
            placeholders: A list or dictionary with placeholders.

        Returns:
            A pandas compatible frequency string or None if the superior time
            resolution is higher than a year.
        """
        # All placeholders from which we know the resolution:
        placeholders = set(placeholders).intersection(
            Dataset._temporal_resolution
        )

        if not placeholders:
            return None

        highest_resolution = max(
            (Dataset._temporal_resolution[tp] for tp in placeholders),
        )

        highest_resolution_index = list(
            Dataset._temporal_resolution.values()).index(highest_resolution)

        if highest_resolution_index == 0:
            return None

        resolutions = list(Dataset._temporal_resolution.values())
        superior_resolution = resolutions[highest_resolution_index - 1]

        return pd.Timedelta(superior_resolution).to_pytimedelta()

    @staticmethod
    def _get_time_resolution(path_or_dict, highest=True):
        """Get the lowest/highest time resolution of all placeholders

        Seconds have a higher time resolution than minutes, etc. If our path
        contains seconds, minutes and hours, this will return a timedelta
        object representing 1 second if *highest* is True otherwise 1 hour.

        Args:
            path_or_dict: A path or dictionary with placeholders.
            highest: If true, search for the highest time resolution instead of
                the lowest.

        Returns:
            The placeholder name with the lowest / highest resolution and
            the representing timedelta object.
        """
        if isinstance(path_or_dict, str):
            placeholders = set(re.findall("\{(\w+)\}", path_or_dict))
            if "doy" in placeholders:
                placeholders.remove("doy")
                placeholders.add("day")
            if "year2" in placeholders:
                placeholders.remove("year2")
                placeholders.add("year")
        else:
            placeholders = set(path_or_dict.keys())

        # All placeholders from which we know the resolution:
        placeholders = set(placeholders).intersection(
            Dataset._temporal_resolution
        )

        if not placeholders:
            # There are no placeholders in the path, therefore we return the
            # highest time resolution automatically
            return "year", Dataset._temporal_resolution["year"]

        # E.g. if we want to find the temporal placeholder with the lowest
        # resolution, we have to search for the maximum of their values because
        # they are represented as timedelta objects, i.e. month > day > hour,
        # etc. expect
        if highest:
            placeholder = min(
                placeholders, key=lambda k: Dataset._temporal_resolution[k]
            )
        else:
            placeholder = max(
                placeholders, key=lambda k: Dataset._temporal_resolution[k]
            )

        return placeholder, Dataset._temporal_resolution[placeholder]

    def _fill_placeholders_with_regexes(self, path, extra_placeholder=None):
        """Fill all placeholders in a path with its RegExes and compile it.

        Args:
            path:
            extra_placeholder:

        Returns:

        """
        if extra_placeholder is None:
            extra_placeholder = {}

        placeholder = {
            **self._time_placeholder,
            **self._user_placeholder,
            **extra_placeholder,
        }

        # Mask all dots and convert the asterisk to regular expression syntax:
        path = path.replace(".", "\.").replace("*", ".*?")

        # Python's standard regex module (re) cannot handle multiple groups
        # with the same name. Hence, we need to cover duplicated placeholders
        # so that only the first of them does group capturing.
        path_placeholders = re.findall("\{(\w+)\}", path)
        duplicated_placeholders = {
            p: self._remove_group_capturing(p, placeholder[p])
            for p in path_placeholders if path_placeholders.count(p) > 1
        }

        if duplicated_placeholders:
            for p, v in duplicated_placeholders.items():
                split_index = path.index("{"+p+"}") + len(p) + 2

                # The value of the placeholder might contain a { or } as regex.
                # We have to escape them because we use the formatting function
                # later.
                v = v.replace("{", "{{").replace("}", "}}")

                changed_part = path[split_index:].replace("{" + p + "}", v)
                path = path[:split_index] + changed_part
        try:
            # Prepare the regex for the template, convert it to an exact match:
            regex = "^" + path.format(**placeholder) + "$"
        except KeyError as err:
            raise UnknownPlaceholderError(self.name, err.args[0])
        except ValueError as err:
            raise PlaceholderRegexError(self.name, str(err))

        return re.compile(regex)

    @staticmethod
    def _complete_placeholders_regex(placeholder):
        """Complete placeholders' regexes to capture groups.

        Args:
            placeholder: A dictionary of placeholders and their matching
            regular expressions

        Returns:

        """

        return {
            name: Dataset._add_group_capturing(name, value)
            for name, value in placeholder.items()
        }

    @staticmethod
    def _add_group_capturing(placeholder, value):
        """Complete placeholder's regex to capture groups.

        Args:
            placeholder: A dictionary of placeholders and their matching
            regular expressions

        Returns:

        """
        if value is None:
            return None
        elif isinstance(value, (tuple, list)):
            return f"(?P<{placeholder}>{'|'.join(value)})"
        else:
            return f"(?P<{placeholder}>{value})"

    @staticmethod
    def _remove_group_capturing(placeholder, value):
        if f"(?P<{placeholder}>" not in value:
            return value
        else:
            # The last character is the closing parenthesis:
            return value[len(f"(?P<{placeholder}>"):-1]

    @expects_file_info()
    def read(self, file_info=None, **read_args):
        """Open and read a file.

        Notes:
            You need to specify a file handler for this dataset before you
            can use this method.

        Args:
            file_info: A string, path-alike object or a
                :class:`~typhon.spareice.handlers.common.FileInfo` object.
            **read_args: Additional key word arguments for the
                *read* method of the used file handler class.

        Returns:
            The content of the read file.
        """
        if self.handler is None:
            raise NoHandlerError(f"Could not read '{file_info.path}'!")

        read_args = {**self.read_args, **read_args}

        if self._path_extension not in self.handler.handle_compression_formats\
                and self.decompress:
            with typhon.files.decompress(file_info.path) as decompressed_path:
                decompressed_file = file_info.copy()
                decompressed_file.path = decompressed_path
                data = self.handler.read(decompressed_file, **read_args)
        else:
            data = self.handler.read(file_info, **read_args)

        # Add also data from linked datasets:
        if self._link:
            linked_data = []
            for link in self._link.values():
                if link["linker"] is None:
                    # Simply try to find the corresponding file by generating a
                    # filename:
                    other_file = link["target"].generate_filename(
                        times=file_info.times, fill=file_info.attr
                    )
                else:
                    # Find the corresponding file via the given linker function
                    other_file = link["linker"](link["target"], file_info)

                linked_data.append(
                    self._link["target"].read(other_file)
                )

            return self._merge_data([data, *linked_data])

        return data

    def _retrieve_time_coverage(self, filled_placeholder,):
        """Retrieve the time coverage from a dictionary of placeholders.

        Args:
            filled_placeholder: A dictionary with placeholders and their
                fillings.

        Returns:
            A tuple of two datetime objects.
        """
        if not filled_placeholder:
            return None

        start_args, end_args = self._to_datetime_args(filled_placeholder)

        if start_args:
            start_date = datetime(**start_args)
        else:
            start_date = None

        if end_args:
            end_args = {**start_args, **end_args}
            end_date = datetime(**end_args)

            # Sometimes the filename does not explicitly provide the complete
            # end date. Imagine there is only hour and minute given, then day
            # change would not be noticed. Therefore, make sure that the end
            # date is always bigger (later) than the start date.
            if end_date < start_date:
                end_date += self._end_time_superior
        else:
            end_date = None

        return start_date, end_date

    def save_info_cache(self, filename):
        """ Saves information cache to a file.

        Returns:
            None
        """
        if filename is not None:
            # First write all to a backup file. If something happens, only the
            # backup file will be overwritten.
            with open(filename+".backup", 'w') as file:
                # We cannot save datetime objects with json directly. We have
                # to convert them to strings first:
                info_cache = [
                    info.to_json_dict()
                    for info in self.info_cache.values()
                ]
                json.dump(info_cache, file)

            # Then rename the backup file
            shutil.move(filename+".backup", filename)

    def set_placeholders(self, **placeholders):
        """Set placeholders for this Dataset.

        Args:
            **placeholders: Placeholders as keyword arguments.

        Returns:
            None
        """

        self._user_placeholder.update(
            self._complete_placeholders_regex(placeholders)
        )

        # Update the path regex (uses automatically the user-defined
        # placeholders):
        self._path_regex = self._fill_placeholders_with_regexes(self.path)

    @property
    def time_coverage(self):
        """Get and set the time coverage of the files of this dataset

        Setting the time coverage after initialisation resets the info cache of
        the dataset object.

        Returns:
            The time coverage of the whole dataset (if it is a single file) as
            tuple of datetime objects or (if it is a multi file dataset) the
            fixed time duration of each file as timedelta object.

        """
        return self._time_coverage

    @time_coverage.setter
    def time_coverage(self, value):
        if self.single_file:
            if value is None:
                # The default for single file datasets:
                self._time_coverage = [
                    datetime.min,
                    datetime.max
                ]
            else:
                self._time_coverage = [
                    to_datetime(value[0]),
                    to_datetime(value[1]),
                ]
        elif value is not None:
            self._time_coverage = to_timedelta(value)
        else:
            self._time_coverage = None

        # Reset the info cache because some file information may have changed
        # now
        self.info_cache = {}

    def write(self, data, file_info=None, times=None, fill=None,
              in_background=False, **write_args):
        """Write content to a file by using the Dataset's file handler.

        If the filename extension is a compression format (such as *zip*,
        etc.) and *Dataset.compress* is set to true, the file will be
        compressed afterwards.

        Notes:
            You need to specify a file handler for this dataset before you
            can use this method.

        Args:
            data: An object that can be stored by the used file handler class.
            file_info: A string, path-alike object or a
                :class:`~typhon.spareice.handlers.common.FileInfo` object.
            in_background: If true (default), this runs the writing process in
                a background thread so it does not pause the main process.
            **write_args: Additional key word arguments for the *write* method
                of the used file handler object.

        Returns:
            None

        Examples:

        .. code-block:: python

            import matplotlib.pyplot as plt
            from typhon.spareice.datasets import Dataset
            from typhon.spareice.handlers import Plotter

            # Define a dataset consisting of multiple files:
            plots = Dataset(
                path="/dir/{year}/{month}/{day}/{hour}{minute}{second}.png",
                handler=Plotter,
            )

            # Let's create a plot example
            fig, ax = plt.subplots()
            ax.plot([0, 1], [0, 1])
            ax.set_title("Data from 2018-01-01")

            ## To save the plot as a file of the dataset, you have two options:
            # Use this simple expression:
            plots["2018-01-01"] = fig

            # OR use write in combination with generate_filename
            filename = plots.generate_filename("2018-01-01")
            plots.write(fig, filename)

            # Hint: If saving the plot takes a lot of time but you want to
            # continue with the program in the meanwhile, you can use the
            # *in_background* option. This saves the plot in a background
            # thread.
            plots.write(fig, filename, in_background=True)

            # continue with other stuff immediately and do not wait until the
            # plot is saved...
            do_other_stuff(...)

        """

        if file_info is None:
            if times is None:
                raise ValueError(
                    "Either the argument file_info or times must be given!")
            else:
                file_info = FileInfo(
                    self.generate_filename(times, fill=None), times, fill
                )
        elif times is not None:
            raise ValueError(
                "Either the argument file_info or times must be given!")
        elif isinstance(file_info, str):
            file_info = FileInfo(file_info)

        if self.handler is None:
            raise NoHandlerError(
                f"Could not write data to '{file_info.path}'!"
            )

        if in_background:
            # Run this function again but as a background thread in a queue:
            threading.Thread(
                target=Dataset.write, args=(self, data, file_info),
                kwargs=write_args.update(in_background=False),
            ).start()
            return

        write_args = {**self.write_args, **write_args}

        # The users should not be bothered with creating directories by
        # themselves.
        os.makedirs(os.path.dirname(file_info), exist_ok=True)

        if self.compress:
            with typhon.files.compress(file_info.path) as compressed_path:
                compressed_file = file_info.copy()
                compressed_file.path = compressed_path
                self.handler.write(data, compressed_file, **write_args)
        else:
            self.handler.write(data, file_info, **write_args)

    def writing_complete(self):
        """Check whether all writing threads are finished.

        Returns:
            True if all writing threads are done.
        """
        return self._write_queue.empty()


class DataSlider:
    """Join and align data from different datasets

    Works only with Datasets which read return values are GroupedArrays.
    """

    def __init__(
            self, start, end, *datasets,):
        """Initialise a DataSlider object

        Args:
            *datasets: A list / tuple of a datasets that should be iterated,
                which read-method returns such an array set.
        """

        self.start = None if start is None else to_datetime(start)
        self.end = None if end is None else to_datetime(end)

        self._cache = {}
        self._current_end = None

        # In this container will only sources be saved that are 'collectable',
        # i.e. Dataset objects. Static sources as arrays will be directly saved
        # to the cache.
        self.datasets = datasets

    def __iter__(self):
        return iter(self.move())

    def add(self, source, name=None):
        """Add a new source to this data slider

        Args:
            source: A dict-like array set (e.g. xarray.Dataset) or a Dataset
                object.
            name:

        Returns:
            None
        """
        if not isinstance(source, Dataset):
            raise ValueError(
                f"Source of type {type(source)} is not a Dataset-like object!"
            )

        self.datasets.append(source)

    def move(self):
        primary = self.datasets[0]
        primary_files = primary.icollect(
            self.start, self.end, return_info=True
        )
        for primary_file, primary_data in primary_files:
            # We add the primary data later:
            data = {}
            files = {primary.name: [primary_file]}

            if len(self.datasets) > 1:
                for secondary in self.datasets[1:]:
                    # Get the corresponding secondary files:
                    # TODO: Use caching to avoid multiple reading of the same
                    # files
                    secondary_files, secondary_data = secondary.collect(
                        *primary_file.times, return_info=True, concat=False,
                    )
                    data[secondary.name] = GroupedArrays.concat(
                        secondary_data
                    )
                    files[secondary.name] = secondary_files

            # data = self._align_to_primary(data, primary_data)
            data[primary.name] = primary_data

            yield files, data

    def _align_to_primary(self, data, primary):
        primary_start, primary_end = primary.get_range("time")

        for name, dataset in data.items():
            indices = (primary_start <= dataset["time"]) \
                      & (dataset["time"] <= primary_end)
            data[name] = dataset[indices]

    def _fetch_cache_update(self, sources=None):
        data_dict = {}
        try:
            for name in self._fetcher:
                if (name in self._cache and
                        self._current_end < self._cache[name]["time"].max()):
                    continue

                # Fetch the data from the dataset:
                file, data = next(self._fetcher[name])

                if "__original_file" not in data:
                    # Set where the file came from:
                    data.attrs["__original_file"] = file.path
                data_dict[name] = data
        except StopIteration:
            # One source has no data left
            raise StopIteration
        except Exception as err:
            # TODO: We could think of catching errors here, but what would
            # TODO: we do against a infinite while loop?
            raise err

        return data_dict

    def _select_common_time(self, data, start, end):
        """Select only the time window where all time series have data
        """

        common_start = np.max(
            [data["time"].min() for data in cache.values()]
        )
        common_start = max(common_start, self.start, self._current_end)

        common_end = np.min(
            [data["time"].max() for data in cache.values()]
        )
        common_end = min(common_end, self.end)

        # Return the selection
        return {
            name: data[
                (data["time"] >= common_start)
                & (data["time"] <= common_end)
            ]
            for name, data in cache.items()
        }, common_end

    def flush(self):
        """Return the data of all sources at once.

        Returns:
            A dictionary of array objects (numpy, GroupedArrays, xarray, etc.)
        """
        fetched_data = {
            name: GroupedArrays.concat(source.collect(self.start, self.end))
            for name, source in self.sources.items()
        }

        # Reset the current time:
        self._current_end = datetime.min

        # The static data is already in the cache:
        return self._select_common_time({**self._cache, **fetched_data})[0]


class DatasetManager(dict):
    def __init__(self, *args, **kwargs):
        """Simple container for multiple Dataset objects.

        You can use it as a native dictionary.

        More functionality will be added in future.

        Example:

        .. code-block:: python

            datasets = DatasetManager()

            datasets += Dataset(
                name="images",
                files="path/to/files.png",
            )

            # do something with it
            for name, dataset in datasets.items():
                dataset.find(...)

        """
        super(DatasetManager, self).__init__(*args, **kwargs)

    def __iadd__(self, dataset):
        if dataset.name in self:
            warnings.warn(
                "DatasetManager: Overwrite dataset with name '%s'!"
                % dataset.name, RuntimeWarning)

        self[dataset.name] = dataset
        return self
