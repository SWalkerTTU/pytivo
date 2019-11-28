This fork is the python 3 port of pyTivo!

# Description

pyTivo lets you stream most videos from your PC to your unhacked tivo. 
It uses the HMO server protocol. It will guess if your video is 4:3 or 
16:9 and pad your video if it thinks it is needed. It will not transcode 
an mpeg that is supported by your tivo.

# Installation

This should work with macOS, Linux, and Windows.

You need python 3.6 or greater installed on your system.

## "Easy" Install Instructions

The easiest way to install is to have the pipx package installed for your
python
```bash
pip install pipx
```

This fork of pytivo is a python package, installable via `pip`, and thus also
installable with `pipx`. `pipx` isolates the python environment for pytivo from
the rest of your system python, and puts the `pytivo` app executable in your
user binary path.

```bash
pipx install --spec https://github.com/itsayellow/pytivo.git pytivo
```

After using this pipx install method, you should be able to execute the
`pytivo` command from your shell.

## Editing pytivo.conf

You need a valid `pyTivo.conf` file in either the directory where you execute
`pytivo` (i.e. your current working directory), or in `/etc/pyTivo.conf`, or in
a path specified with the `-c` option to pytivo.

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

## Windows Notes

I have limited means of testing on Windows, and this fork of pytivo has been
heavily revamped to port it to python 3.  Windows functionality therefore may
be broken.  Also, the advanced Windows-only features below may not work.  I
still include the notes below for reference.

install pywin32 (only to install as a service) - 
[http://sourceforge.net/project/showfiles.php?group_id=78018&package_id=79063]()
- Windows users only and only if you intend to install as a service

```bash
pip install pywin32
```

### To Use as a Service in Windows

#### To Install Service

run pyTivoService.py --startup auto install

#### To remove service

run pyTivoService.py remove

# Credits
pyTivo was created by Jason Michalski ("armooo"). Contributors include 
Kevin R. Keegan, William McBrine, and Terry Mound ("wgw").

This conversion to python 3 and refactoring was carried out by Matthew A. Clapp
("itsayellow")
