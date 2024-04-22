# SPDX-FileCopyrightText: 2024 SUSE LLC
#
# SPDX-License-Identifier: LGPL-2.1-or-later

from setuptools import setup

setup(
    name="vms",
    version="0.0.1",
    py_modules=["vms"],
    install_requires=[
        "Click",
        "libvirt-python",
        "rich",
    ],
    entry_points={
        "console_scripts": [
            "vms = vms:cli"
        ]
    }
)
