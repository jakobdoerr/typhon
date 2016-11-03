# -*- coding: utf-8 -*-

"""Utility functions related to plotting.
"""
import os
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
import numpy as np
from warnings import warn

__all__ = ['mpl_colors',
           'cmap2txt',
           'cmap2cpt',
           'cmap2act',
           'cmap2c3g',
           'cmap2ggr',
           'cmap_from_act',
           ]


def mpl_colors(cmap=None, n=10):
    """Return a list of RGB values.

    Parameters:
        cmap (str): Name of a registered colormap
        n (int): Number of colors to return

    Returns:
        np.array: Array with RGB and alpha values.

    Examples:
        >>> mpl_colors('viridis', 5)
        array([[ 0.267004,  0.004874,  0.329415,  1.      ],
            [ 0.229739,  0.322361,  0.545706,  1.      ],
            [ 0.127568,  0.566949,  0.550556,  1.      ],
            [ 0.369214,  0.788888,  0.382914,  1.      ],
            [ 0.993248,  0.906157,  0.143936,  1.      ]])
    """
    if cmap is None:
        cmap = plt.rcParams['image.cmap']

    return plt.get_cmap(cmap)(np.linspace(0, 1, n))


def cmap2txt(cmap, filename=None, N=256, comments='%'):
    """Export colormap to txt file.

    Parameters:
        cmap (str): Colormap name.
        filename (str): Optional filename.
            Default: cmap + '.txt'
        comments (str): Character to start comments with.
        N (int): Number of colors.

    """
    colors = mpl_colors(cmap, N)
    header = 'Colormap "{}"'.format(cmap)

    if filename is None:
        filename = cmap + '.txt'

    np.savetxt(filename, colors[:, :3], header=header, comments=comments)


def cmap2cpt(cmap, filename=None, N=256):
    """Export colormap to cpt file.

    Parameters:
        cmap (str): Colormap name.
        filename (str): Optional filename.
            Default: cmap + '.cpt'
        N (int): Number of colors.

    """
    colors = mpl_colors(cmap, N)
    header = ('# GMT palette "{}"\n'
              '# COLOR_MODEL = RGB\n'.format(cmap))

    left = '{:>3d} {:>3d} {:>3d} {:>3d}  '.format
    right = '{:>3d} {:>3d} {:>3d} {:>3d}\n'.format

    if filename is None:
        filename = cmap + '.cpt'

    with open(filename, 'w') as f:
        f.write(header)

        # For each level specify a ...
        for n in range(len(colors)):
            rgb = [int(c * 255) for c in colors[n, :3]]
            # ... start color ...
            f.write(left(n, *rgb))
            # ... and end color.
            f.write(right(n + 1, *rgb))


def cmap2act(cmap, filename=None, N=255):
    """Export colormap to Adobe Color Table file.

    Parameters:
        cmap (str): Colormap name.
        filename (str): Optional filename.
            Default: cmap + '.cpt'
        N (int): Number of colors.

    """
    if filename is None:
        filename = cmap + '.act'

    if N > 256:
        N = 256
        warn('Maximum number of colors is 256.')

    colors = mpl_colors(cmap, N)[:, :3]

    rgb = np.zeros(256 * 3 + 2)
    rgb[:colors.size] = (colors.flatten() * 255).astype(np.uint8)
    rgb[768:770] = np.uint8(N // 2**8), np.uint8(N % 2**8)

    rgb.astype(np.uint8).tofile(filename)


def cmap2c3g(cmap, filename=None, N=256):
    """Export colormap ass CSS3 gradient.

    Parameters:
        cmap (str): Colormap name.
        filename (str): Optional filename.
            Default: cmap + '.cpt'
        N (int): Number of colors.

    """
    if filename is None:
        filename = cmap + '.c3g'

    colors = mpl_colors(cmap, N)

    header = (
        '/*'
        '   CSS3 Gradient "{}"\n'
        '*/\n\n'
        'linear-gradient(\n'
        '  0deg,\n'
        ).format(cmap)

    color_spec = '  rgb({:>3d},{:>3d},{:>3d}) {:>8.3%}'.format

    with open(filename, 'w') as f:
        f.write(header)

        ncolors = len(colors)
        for n in range(ncolors):
            r, g, b = [int(c * 255) for c in colors[n, :3]]
            f.write(color_spec(r, g, b, n / (ncolors - 1)))
            if n < ncolors - 1:
                f.write(',\n')

        f.write('\n  );')


def cmap2ggr(cmap, filename=None, N=256):
    """Export colormap as GIMP gradient.

    Parameters:
        cmap (str): Colormap name.
        filename (str): Optional filename.
            Default: cmap + '.cpt'
        N (int): Number of colors.

    """
    if filename is None:
        filename = cmap + '.ggr'

    colors = mpl_colors(cmap, N)
    header = ('GIMP Gradient\n'
              'Name: {}\n'
              '{}\n').format(cmap, len(colors) - 1)

    line = ('{:.6f} {:.6f} {:.6f} '  # start, middle, stop
            '{:.6f} {:.6f} {:.6f} {:.6f} '  # RGBA
            '{:.6f} {:.6f} {:.6f} {:.6f} '  # RGBA next level
            '0 0\n').format

    def idx(x):
        return x / (len(colors) - 1)

    with open(filename, 'w') as f:
        f.write(header)

        for n in range(len(colors) - 1):
            rgb = colors[n, :]
            rgb_next = colors[n + 1, :]
            f.write(line(idx(n), idx(n + 0.5), idx(n + 1), *rgb, *rgb_next))


def cmap_from_act(file, name=None):
    """Import colormap from Adobe Color Table file.

    Parameters:
        file (str): Path to act file.
        name (str): Colormap name. Defaults to filename without extension.

    Returns:
        LinearSegmentedColormap.
    """
    # Extract colormap name from filename.
    if name is None:
        name = os.path.splitext(os.path.basename(file))[0]

    # Read binary file and determine number of colors
    rgb = np.fromfile(file, dtype=np.uint8)
    if rgb.shape[0] >= 770:
        ncolors = rgb[768] * 2**8 + rgb[769]
    else:
        ncolors = 256

    colors = rgb[:ncolors*3].reshape(ncolors, 3) / 255
    cmap = LinearSegmentedColormap.from_list(name, colors, N=ncolors)

    plt.register_cmap(cmap=cmap)  # Register colormap.

    return cmap
