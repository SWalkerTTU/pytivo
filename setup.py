# setup for datadiary

import sys
from setuptools import setup, find_packages

# here = os.path.abspath(os.path.dirname(__file__))

# extra windows-only executable
if sys.platform == "win32":
    extra_console_scripts = ["pytivoservice=pytivo.pyTivoService:cli"]
else:
    extra_console_scripts = []

setup(
    name="pytivo",
    version="0.1.0",
    description=(
        "TiVo HMO and GoBack server.  Used to serve videos and other media "
        "to a TiVo from a computer."
    ),
    author="Matthew A. Clapp",
    author_email="itsayellow+dev@gmail.com",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Programming Language :: Python :: 3",
    ],
    keywords="tivo",
    url="https://github.com/itsayellow/pytivo",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    package_data={
        # in any package, include template dirs and any files within
        "": ["templates/*"]
    },
    install_requires=[
        "mutagen",
        "Cheetah3",
        "zeroconf",
        "Pillow",
        "pywin32;platform_system=='Windows'",
    ],
    entry_points={
        "console_scripts": ["pytivo=pytivo.main:cli"] + extra_console_scripts
    },
    python_requires=">=3.6",
)
