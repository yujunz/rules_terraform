from __future__ import print_function

import argparse
import json
import logging
import os

import httplib2
import sys
import yaml
from containerregistry.client import docker_creds_ as docker_creds
from containerregistry.client import docker_name_ as docker_name
from containerregistry.client.v2 import docker_image_ as v2_image
from containerregistry.client.v2_2 import docker_image_ as v2_2_image
from containerregistry.client.v2_2 import docker_session_ as v2_2_session
from containerregistry.tools import patched_ as patched
from containerregistry.transport import transport_pool_ as transport_pool

parser = argparse.ArgumentParser(
    description='Resolve image references to digests.')

parser.add_argument(
    '--template', action='append',
    help='The template file to resolve.')

parser.add_argument(
    '--image_spec', action='append',
    help='Associative lists of the constitutent elements of a FromDisk image.')

parser.add_argument(
    '--image_chroot', action='store',
    help=('The repository under which to chroot image references when '
          'publishing them.'))

parser.add_argument(
    '--image_tag', action='store',
    help=('The tag to use when publishing images.'))

parser.add_argument(
    '--output_format', action='store', default="json",
    choices=["yaml", "json", "yml"],
    help=('Output format. One of yaml,json'))

_THREADS = 32


def Resolve(input, string_to_digest):
    """Translate tag references within the input yaml into digests."""

    def walk_dict(d):
        return {
            walk(k): walk(v)
            for (k, v) in d.iteritems()
        }

    def walk_list(l):
        return [walk(e) for e in l]

    def walk_string(s):
        try:
            return string_to_digest(s)
        except:
            return s

    def walk(o):
        if isinstance(o, dict):
            return walk_dict(o)
        if isinstance(o, list):
            return walk_list(o)
        if isinstance(o, str):
            return walk_string(o)
        return o

    return map(walk, yaml.load_all(input))


def StringToDigest(string, overrides, transport):
    """Turn a string into a stringified digest."""
    if string in overrides:
        return str(overrides[string])

    # Attempt to turn the string into a tag, this may throw.
    tag = docker_name.Tag(string)

    def fully_qualify_digest(digest):
        return docker_name.Digest('{registry}/{repo}@{digest}'.format(
            registry=tag.registry, repo=tag.repository, digest=digest))

    # Resolve the tag to digest using the standard
    # Docker keychain logic.
    creds = docker_creds.DefaultKeychain.Resolve(tag)
    with v2_2_image.FromRegistry(tag, creds, transport) as img:
        if img.exists():
            digest = fully_qualify_digest(img.digest())
            overrides[string] = digest
            return str(digest)

    # If the tag doesn't exists as v2.2, then try as v2.
    with v2_image.FromRegistry(tag, creds, transport) as img:
        digest = fully_qualify_digest(img.digest())
        overrides[string] = digest
        return str(digest)


def Publish(transport, image_chroot,
            name=None, tarball=None, config=None, digest=None, layer=None):
    if not name:
        raise Exception('Expected "name" kwarg')

    if not config and (layer or digest):
        raise Exception(
            name + ': Using "layer" or "digest" requires "config" to be specified.')

    if config:
        with open(config, 'r') as reader:
            config = reader.read()
    elif tarball:
        with v2_2_image.FromTarball(tarball) as base:
            config = base.config_file()
    else:
        raise Exception(name + ': Either "config" or "tarball" must be specified.')

    if digest or layer:
        digest = digest.split(',')
        layer = layer.split(',')
        if len(digest) != len(layer):
            raise Exception(
                name + ': "digest" and "layer" must have matching lengths.')
    else:
        digest = []
        layer = []

    name_to_replace = name
    if image_chroot:
        name_to_publish = docker_name.Tag(os.path.join(image_chroot, name), strict=False)
    else:
        # Without a chroot, the left-hand-side must be a valid tag.
        name_to_publish = docker_name.Tag(name, strict=False)

    # Resolve the appropriate credential to use based on the standard Docker
    # client logic.
    creds = docker_creds.DefaultKeychain.Resolve(name_to_publish)

    with v2_2_session.Push(name_to_publish, creds, transport, threads=_THREADS) as session:
        with v2_2_image.FromDisk(config, zip(digest or [], layer or []),
                                 legacy_base=tarball) as v2_2_img:
            session.upload(v2_2_img)

            return (name_to_replace, docker_name.Digest('{repository}@{digest}'.format(
                repository=name_to_publish.as_repository(),
                digest=v2_2_img.digest())))


def main():
    args = parser.parse_args()

    transport = transport_pool.Http(httplib2.Http, size=_THREADS)

    unseen_strings = set()
    overrides = {}
    # TODO(mattmoor): Execute these in a threadpool and
    # aggregate the results as they complete.
    for spec in args.image_spec or []:
        parts = spec.split(';')
        kwargs = dict([x.split('=', 2) for x in parts])
        try:
            (tag, digest) = Publish(transport, args.image_chroot, **kwargs)
            overrides[tag] = digest
            unseen_strings.add(tag)
        except Exception as e:
            logging.fatal('Error publishing provided image: %s', e)
            sys.exit(1)

    def _StringToDigest(t):
        if t in unseen_strings:
            unseen_strings.remove(t)
        return StringToDigest(t, overrides, transport)

    outputs = []

    for path in args.template:
        with open(path, 'r') as f:
            if path.endswith(".json"):
                inputs = json.dumps(json.load(f))
            else:
                inputs = f.read()
        outputs.extend(Resolve(inputs, _StringToDigest))

    if len(outputs) == 0:
        print("ERROR: Nothing to resolve (Are you sure the input has valid json/yaml objects?)")
        sys.exit(1)

    if len(unseen_strings) > 0:
        print('ERROR: The following image references were not found:', file=sys.stderr)
        for ref in unseen_strings:
            print('    %s' % ref, file=sys.stderr)
        sys.exit(1)

    if args.output_format == "json":
        # pretty-print if there's only one json object
        if len(outputs) == 1:
            print(json.dumps(outputs[0], indent=2))
        else:
            for o in outputs:
                print(json.dumps(o))
    elif args.output_format in ["yaml", "yml"]:
        yaml.dump_all(outputs, sys.stdout, default_flow_style=False)

    else:
        raise ValueError("Unknown output format %s" % args.output_format)


if __name__ == '__main__':
    with patched.Httplib2():
        main()