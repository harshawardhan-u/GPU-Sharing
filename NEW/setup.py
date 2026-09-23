from setuptools import setup

setup(
    name='cloud-run-cli',
    version='1.0',
    py_modules=['cloud_run'], # Points to your cloud_run.py file
    entry_points={
        'console_scripts': [
            # This is the magic line. It creates the 'cloud-run' command!
            'cloud-run=cloud_run:main',
        ],
    },
    install_requires=[
        'requests'
    ]
)