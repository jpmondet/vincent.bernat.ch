# -*- coding: utf-8 -*-
"""
Contains classes to handle images related things

# Requires PIL/Pillow
"""

from hyde.plugin import Plugin
from hyde.plugin import CLTransformer
from fswrap import File, Folder

import xml.etree.ElementTree as ET
from pyquery import PyQuery as pq
from lxml.html import tostring as html2str
import lxml.html

from PIL import Image
from pymediainfo import MediaInfo
import new
import os
import re
import urllib
from functools import partial

# Patch lxml.html to consider "source" and "track" as void elements
# (doesn't work).
#lxml.html.defs.empty_tags = frozenset(
#    list(lxml.html.defs.empty_tags) +
#    ['track', 'source'])

class Thumb(object):
    def __init__(self, path, **kwargs):
        self.path = path
        for arg in kwargs:
            setattr(self, arg, kwargs[arg])
    def __str__(self):
        return self.path

def thumb(self, defaults={}, width=None, height=None):
    """
    Generate a thumbnail for the given image
    """
    if width is None and height is None:
        width, height = defaults['width'], defaults['height']
    im = Image.open(self.path)
    if im.mode != 'RGBA':
        im = im.convert('RGBA')
    # Convert to a thumbnail
    if width is None:
        # height is not None
        width = im.size[0]*height/im.size[1] + 1
    elif height is None:
        # width is not None
        height = im.size[1]*width/im.size[0] + 1
    im.thumbnail((width, height), Image.ANTIALIAS)
    # Prepare path
    path = os.path.join(os.path.dirname(self.get_relative_deploy_path()),
                        "%s%s" % (defaults['prefix'],
                                  self.name))
    target = File(Folder(self.site.config.deploy_root_path).child(path))
    target.parent.make()
    if self.name.endswith(".jpg"):
        im.save(target.path, "JPEG", optimize=True, quality=75)
    else:
        im.save(target.path, "PNG", optimize=True)
    return Thumb(path, width=im.size[0], height=im.size[1])

class ImageThumbnailsPlugin(Plugin):
    """
    Provide a function to get thumbnail for any image resource.

    Each image resource will get a `thumb()` function. This function
    can take the following keywords:
      - width (int)
      - height (int)
      - prefix (string): prefix to use for thumbnails

    This plugin can be configured with the exact same keywords to set
    site defaults. The `thumb()` function will return a path to the
    thumbnail. This path will have `width` and `height` as an
    attribute.

    Thumbnails are created in the same directory of their image.

    Currently, only supports PNG and JPG.
    """

    def __init__(self, site):
        super(ImageThumbnailsPlugin, self).__init__(site)

    def begin_site(self):
        """
        Find any image resource to add them the thumb() function.
        """
        # Grab default values from config
        config = self.site.config
        defaults = { "width": None,
                     "height": 40,
                     "prefix": 'thumb_',
                     }
        if hasattr(config, 'thumbnails'):
            defaults.update(config.thumbnails)
        # Make the thumbnailing function
        thumbfn = partial(thumb, defaults=defaults)
        thumbfn.__doc__ = "Create a thumbnail for this image"

        # Add it to any image resource
        for node in self.site.content.walk():
            for resource in node.resources:
                if resource.source_file.kind not in ["jpg", "png"]:
                    continue
                self.logger.debug("Adding thumbnail function to [%s]" % resource)
                resource.thumb = new.instancemethod(thumbfn, resource, resource.__class__)


class JPEGTranPlugin(CLTransformer):
    """
    The plugin class for JPEGTran
    """

    def __init__(self, site):
        super(JPEGTranPlugin, self).__init__(site)

    @property
    def plugin_name(self):
        """
        The name of the plugin.
        """
        return "jpegtran"

    def option_prefix(self, option):
        return "-"

    def binary_resource_complete(self, resource):
        """
        Run jpegtran to compress the jpg file.
        """

        if not resource.source_file.kind == 'jpg':
            return

        supported = [
            "optimize",
            "progressive",
            "restart",
            "arithmetic",
            "perfect",
            "copy",
        ]
        source = File(self.site.config.deploy_root_path.child(
                resource.relative_deploy_path))
        target = File.make_temp('')
        jpegtran = self.app
        args = [unicode(jpegtran)]
        args.extend(self.process_args(supported))
        args.extend(["-outfile", unicode(target), unicode(source)])
        self.call_app(args)
        target.copy_to(source)
        target.delete()


class ImageFixerPlugin(Plugin):
    """Fix images in various ways:

      - add width/height attributes
      - make them responsive
      - turn them into object if they are interactive
      - turn them into video if they are video
    """

    def __init__(self, site):
        super(ImageFixerPlugin, self).__init__(site)
        self.cache = {}

    def _topx(self, x):
        """Convert a size to pixels."""
        mo = re.match(r'(?P<size>\d+(?:\.\d*)?)(?P<unit>.*)', x)
        if not mo:
            raise ValueError("cannot convert {} to pixel".format(x))
        unit = mo.group("unit")
        size = float(mo.group("size"))
        if unit in {"", "px"}:
            return int(size)
        if unit == "pt":
            return int(size*4/3)
        raise ValueError("unknown unit {}".format(unit))

    def _size(self, image):
        """Get size for an object."""
        if image.source_file.kind in {'png', 'jpg', 'jpeg', 'gif'}:
            return Image.open(image.path).size
        if image.source_file.kind in {'svg'}:
            svg = ET.parse(image.path).getroot()
            return tuple(x and self._topx(x) or None
                         for x in (svg.attrib.get('width', None),
                                   svg.attrib.get('height', None)))
        if image.source_file.kind in {'m3u8'}:
            with open(image.path) as f:
                w, h = max([(int(w), int(h))
                            for w, h in re.findall('RESOLUTION=(\d+)x(\d+)(?:$|,)',
                                                   f.read())])
                return (w, h)
        if image.source_file.kind in {'mp4', 'ogv'}:
            mi = MediaInfo.parse(image.path)
            track = [t for t in mi.tracks if t.track_type == 'Video'][0]
            return (track.width, track.height)
        self.logger.warn(
            "[%s] has an img tag not linking to an image" % resource)
        return (None, None)

    def _resize(self, resource, src, width, height):
        """Determine size of an img tag."""
        if src not in self.cache:
            if src.startswith(self.site.config.media_url):
                path = src[len(self.site.config.media_url):].lstrip("/")
                path = self.site.config.media_root_path.child(path)
                image = self.site.content.resource_from_relative_deploy_path(
                    path)
            elif re.match(r'([a-z]+://|//).*', src):
                # Not a local link
                return None
            elif src.startswith("/"):
                # Absolute resource
                path = src.lstrip("/")
                image = self.site.content.resource_from_relative_deploy_path(
                    path)
            else:
                # Relative resource
                path = resource.node.source_folder.child(src)
                image = self.site.content.resource_from_path(path)
            if image is None:
                self.logger.warn(
                    "[%s] has an unknown image %s" % (resource, src))
                return None
            self.cache[src] = self._size(image)
            self.logger.debug("Image [%s] is %s" % (src,
                                                    self.cache[src]))
        new_width, new_height = self.cache[src]
        if new_width is None or new_height is None:
            return None
        if width is not None:
            return (width, int(width) * new_height / new_width)
        elif height is not None:
            return (int(height) * new_width / new_height, height)
        return (new_width, new_height)

    def text_resource_complete(self, resource, text):
        """
        When the resource is generated, search for img tag and fix them.
        """
        if not resource.source_file.kind == 'html':
            return

        d = pq(text)
        for img in d.items('img'):
            width = img.attr.width
            height = img.attr.height
            src = img.attr.src
            src = urllib.unquote(src)
            if src is None:
                self.logger.warn(
                    "[%s] has an img tag without src attribute" % resource)
                continue
            if width is None or height is None:
                wh = self._resize(resource, src, width, height)
                if wh is not None:
                    width, height = wh
                else:
                    width, height = None, None

            # Adapt width/height if this is a scaled image (something@2x.jpg)
            mo = re.match(r'.*@(\d+)x\.[^.]*$', src)
            if mo:
                factor = int(mo.group(1))
                width /= factor
                height /= factor

            # Put new width/height
            if width is not None:
                img.attr.width = '{}'.format(width)
                img.attr.height = '{}'.format(height)

            # If image is a SVG in /obj/, turns into an object
            if "/obj/" in src and src.endswith(".svg"):
                img[0].tag = 'object'
                img.attr("type", "image/svg+xml")
                img.attr("data", src)
                del img.attr.src
                img.text('&#128444; {}'.format(img.attr.alt or ""))

            # On-demand videos (should be in /videos)
            elif src.endswith('.m3u8'):
                id = os.path.splitext(os.path.basename(src))[0]
                img[0].tag = 'video'
                img.attr("controls", "controls")
                img.attr("preload", "none")
                img.attr("poster", self.site.media_url(
                    'images/posters/{}.jpg'.format(id)))
                del img.attr.src
                # Add sources
                m3u8 = pq('<source>')
                m3u8.attr.src = self.site.media_url(
                    'videos/{}.m3u8'.format(id))
                m3u8.attr.type = 'application/vnd.apple.mpegurl'
                img.append(m3u8)
                progressive = pq('<source>')
                progressive.attr.src = 'https://luffy-video.sos-ch-dk-2.exo.io/{}/progressive.mp4'.format(id)
                progressive.attr.type = 'video/mp4; codecs="avc1.4d401f, mp4a.40.2"'
                img.append(progressive)

            # If image is a video not in /videos turn into a simple
            # video tag like an animated GIF.
            elif src.endswith(".mp4") or src.endswith(".ogv"):
                img[0].tag = 'video'
                img.muted = 'muted'
                img.loop = 'loop'
                img.autoplay = 'autoplay'

            # If image is contained in a paragraph, enclose into a
            # responsive structure.
            parent = None
            parents = [p.tag for p in img.parents()]
            if parents[-1] == 'p':
                parent = img.parent()
            elif parents[-2:] == ['p', 'a']:
                parent = img.parent().parent()
            if parent:
                img.addClass('lf-media')
                inner = pq('<span />')
                outer = pq('<div />')
                inner.addClass('lf-media-inner')
                outer.addClass('lf-media-outer')
                if width is not None:
                    inner.css.padding_bottom = '{:.3f}%'.format(
                        float(height)*100./width)
                    outer.css.width = '{}px'.format(width)
                outer.append(inner)

                # If we have a title, also enclose in a figure
                figure = pq('<figure />')
                if img.attr.title:
                    figcaption = pq('<figcaption />')
                    figcaption.text(img.attr.title)
                    del img.attr.title
                    figure.append(outer)
                    figure.append(figcaption)
                else:
                    figure.append(outer)

                # Put image in inner tag
                if img.parent()[0].tag == 'a':
                    inner.append(img.parent())
                else:
                    inner.append(img)
                # Replace parent with our enclosure
                parent.replace_with(figure)

        return html2str(d.root, encoding='unicode')
