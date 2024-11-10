# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
"""Repack model script for training jobs to inject entry points"""
from __future__ import absolute_import

import argparse
import logging
import os
import shutil
import tarfile
import tempfile

# Repack Model
# The following script is run via a training job which takes an existing model and a custom
# entry point script as arguments. The script creates a new model archive with the custom
# entry point in the "code" directory along with the existing model.  Subsequently, when the model
# is unpacked for inference, the custom entry point will be used.
# Reference: https://docs.aws.amazon.com/sagemaker/latest/dg/amazon-sagemaker-toolkits.html

from os.path import abspath, realpath, dirname, normpath, join as joinpath

logger = logging.getLogger(__name__)


def _get_resolved_path(path):
    """Return the normalized absolute path of a given path.

    abspath - returns the absolute path without resolving symlinks
    realpath - resolves the symlinks and gets the actual path
    normpath - normalizes paths (e.g. remove redudant separators)
    and handles platform-specific differences
    """
    return normpath(realpath(abspath(path)))


def _is_bad_path(path, base):
    """Checks if the joined path (base directory + file path) is rooted under the base directory

    Ensuring that the file does not attempt to access paths
    outside the expected directory structure.

    Args:
        path (str): The file path.
        base (str): The base directory.

    Returns:
        bool: True if the path is not rooted under the base directory, False otherwise.
    """
    # joinpath will ignore base if path is absolute
    return not _get_resolved_path(joinpath(base, path)).startswith(base)


def _is_bad_link(info, base):
    """Checks if the link is rooted under the base directory.

    Ensuring that the link does not attempt to access paths outside the expected directory structure

    Args:
        info (tarfile.TarInfo): The tar file info.
        base (str): The base directory.

    Returns:
        bool: True if the link is not rooted under the base directory, False otherwise.
    """
    # Links are interpreted relative to the directory containing the link
    tip = _get_resolved_path(joinpath(base, dirname(info.name)))
    return _is_bad_path(info.linkname, base=tip)


def _get_safe_members(members):
    """A generator that yields members that are safe to extract.

    It filters out bad paths and bad links.

    Args:
        members (list): A list of members to check.

    Yields:
        tarfile.TarInfo: The tar file info.
    """
    base = _get_resolved_path(".")

    for file_info in members:
        if _is_bad_path(file_info.name, base):
            logger.error("%s is blocked (illegal path)", file_info.name)
        elif file_info.issym() and _is_bad_link(file_info, base):
            logger.error("%s is blocked: Symlink to %s", file_info.name, file_info.linkname)
        elif file_info.islnk() and _is_bad_link(file_info, base):
            logger.error("%s is blocked: Hard link to %s", file_info.name, file_info.linkname)
        else:
            yield file_info


def custom_extractall_tarfile(tar, extract_path):
    """Extract a tarfile, optionally using data_filter if available.

    # TODO: The function and it's usages can be deprecated once SageMaker Python SDK
    is upgraded to use Python 3.12+

    If the tarfile has a data_filter attribute, it will be used to extract the contents of the file.
    Otherwise, the _get_safe_members function will be used to filter bad paths and bad links.

    Args:
        tar (tarfile.TarFile): The opened tarfile object.
        extract_path (str): The path to extract the contents of the tarfile.

    Returns:
        None
    """
    if hasattr(tarfile, "data_filter"):
        tar.extractall(path=extract_path, filter="data")
    else:
        tar.extractall(path=extract_path, members=_get_safe_members(tar))


def repack(inference_script, model_archive, dependencies=None, source_dir=None):  # pragma: no cover
    """Repack custom dependencies and code into an existing model TAR archive

    Args:
        inference_script (str): The path to the custom entry point.
        model_archive (str): The name or path (e.g. s3 uri) of the model TAR archive.
        dependencies (str): A space-delimited string of paths to custom dependencies.
        source_dir (str): The path to a custom source directory.
    """

    # the data directory contains a model archive generated by a previous training job
    data_directory = "/opt/ml/input/data/training"
    model_path = os.path.join(data_directory, model_archive.split("/")[-1])

    # create a temporary directory
    with tempfile.TemporaryDirectory() as tmp:
        local_path = os.path.join(tmp, "local.tar.gz")
        # copy the previous training job's model archive to the temporary directory
        shutil.copy2(model_path, local_path)
        src_dir = os.path.join(tmp, "src")
        # create the "code" directory which will contain the inference script
        code_dir = os.path.join(src_dir, "code")
        os.makedirs(code_dir)
        # extract the contents of the previous training job's model archive to the "src"
        # directory of this training job
        with tarfile.open(name=local_path, mode="r:gz") as tf:
            custom_extractall_tarfile(tf, src_dir)

        if source_dir:
            # copy /opt/ml/code to code/
            if os.path.exists(code_dir):
                shutil.rmtree(code_dir)
            shutil.copytree("/opt/ml/code", code_dir)
        else:
            # copy the custom inference script to code/
            entry_point = os.path.join("/opt/ml/code", inference_script)
            shutil.copy2(entry_point, os.path.join(code_dir, inference_script))

        # copy any dependencies to code/lib/
        if dependencies:
            for dependency in dependencies.split(" "):
                actual_dependency_path = os.path.join("/opt/ml/code", dependency)
                lib_dir = os.path.join(code_dir, "lib")
                if not os.path.exists(lib_dir):
                    os.mkdir(lib_dir)
                if os.path.isfile(actual_dependency_path):
                    shutil.copy2(actual_dependency_path, lib_dir)
                else:
                    if os.path.exists(lib_dir):
                        shutil.rmtree(lib_dir)
                    # a directory is in the dependencies. we have to copy
                    # all of /opt/ml/code into the lib dir because the original directory
                    # was flattened by the SDK training job upload..
                    shutil.copytree("/opt/ml/code", lib_dir)
                    break

        # copy the "src" dir, which includes the previous training job's model and the
        # custom inference script, to the output of this training job
        shutil.copytree(src_dir, "/opt/ml/model", dirs_exist_ok=True)


if __name__ == "__main__":  # pragma: no cover
    parser = argparse.ArgumentParser()
    parser.add_argument("--inference_script", type=str, default="inference.py")
    parser.add_argument("--dependencies", type=str, default=None)
    parser.add_argument("--source_dir", type=str, default=None)
    parser.add_argument("--model_archive", type=str, default="model.tar.gz")
    args, extra = parser.parse_known_args()
    repack(
        inference_script=args.inference_script,
        dependencies=args.dependencies,
        source_dir=args.source_dir,
        model_archive=args.model_archive,
    )
