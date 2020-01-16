This fork is the python 3 port of pyTivo!

# Description

pyTivo lets you stream most videos from your PC to your unhacked tivo. 
It uses the HMO server protocol. It will guess if your video is 4:3 or 
16:9 and pad your video if it thinks it is needed. It will not transcode 
an mpeg that is supported by your tivo.

# Installation

This should work with macOS, Linux, and Windows.

You need python 3.6 or greater installed on your system.

For most video operations, you also need [ffmpeg](https://www.ffmpeg.org/) installed on your system.

## "Easy" Install Instructions

This fork of pytivo is a python package, installable via `pip`, and thus also
installable with [pipx](https://github.com/pipxproject/pipx). `pipx` isolates the python environment for pytivo from
the rest of your system python, and puts the `pytivo` app executable in your
user binary path.

Install pipx package with your python
```bash
python3 -m pip install --user pipx
```

Install pytivo using pipx
```bash
pipx install git+https://github.com/itsayellow/pytivo
```

After using this pipx install method, you should be able to execute the
`pytivo` command from your shell.

## Editing pytivo.conf

You need a valid `pyTivo.conf` file in either `/etc/pyTivo.conf`, in your
home directory in `.config/pytivo/pyTivo.conf`, in the directory where you
execute `pytivo` (i.e. your current working directory), or in a path 
specified with the `-c` option to pytivo.

You need to edit pyTivo.conf in at least 2 sections.

1. `ffmpeg=`
2. `[<name of share>]`
    * `path=<full directory path>`
    * `type=<video OR photos OR music>`

ffmpeg should indicate the full path to ffmpeg including filename.

Each `[<name of share>]` describes a folder and type of media to share with
your TiVo.  You can specify multiple shares, even multiple shares of the same
kind of media.  `path` is the absolute path to your media. `type` indicates
whether the share is for `video` (most common) or `photos` or `music`.

# Running pytivo

With the pyTivo.conf file in one of preset config file directories, simply run:
```bash
pytivo
```
from the console.

If the pyTivo.conf file that you want to use is somewhere else, use:
```
pytivo -c <full-path-of-pyTivo.conf>
```

## Windows Service

*Currently the `pytivoservice` tool is not functional.*

Additionally on Windows, there is an extra console utility `pytivoservice` to
install, remove, start, or stop pytivo as a service.

### Install pytivo as a service
```bash
pytivoservice --startup auto install
```

### Remove the pytivo service
```bash
pytivoservice remove
```

### Help on all pytivoservice subcommands
```bash
pytivoservice
```

# Credits
pyTivo was created by Jason Michalski ("armooo"). Contributors include 
Kevin R. Keegan, William McBrine, and Terry Mound ("wgw").

This conversion to python 3 and refactoring was carried out by Matthew A. Clapp
("[itsayellow](https://github.com/itsayellow)")
