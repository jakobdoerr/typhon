# -*- coding: utf-8 -*-
"""Testing the functions in typhon.arts.
"""
import shutil

import pytest

from typhon import arts


class TestPlots:
    """Testing the plot functions."""
    @pytest.mark.skipif(not shutil.which('arts'), reason='arts not in PATH')
    def test_run_arts(self):
        """Test ARTS system call.

        Note: This test is only run, if ARTS is found in PATH.
        """
        arts_out = arts.run_arts(help=True)

        assert arts_out.retcode == 0
