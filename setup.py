from setuptools import setup

setup(
    name="vms",
    version="0.0.1",
    py_modules=["vms"],
    install_requires=[
        "Click",
        "libvirt-python",
        "tabulate",
    ],
    entry_points={
        "console_scripts": [
            "vms = vms:cli"
        ]
    }
)
