#!/usr/bin/env python
"""General utility helper functions.

--------------------------------------------------------------------------------

Readability API - Clean up pages and feeds to be readable.
Copyright (C) 2010  Anthony Lieuallen

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import Cookie
import functools
import logging
import os
import re
import urlparse

from google.appengine.api import memcache
from google.appengine.api import urlfetch
from google.appengine.ext.webapp import template

from third_party import feedparser

EMBED_NAMES = set(('embed', 'object'))
IS_DEV_APPSERVER = 'Development' in os.environ.get('SERVER_SOFTWARE', '')
MAX_SCORE_DEPTH = 5
RE_DOCTYPE = re.compile(r'<!DOCTYPE.*?>', re.S)
RE_HTML_COMMENTS = re.compile(r'<!--.*?-->', re.S)
TAG_NAMES_BLOCK = set(('blockquote', 'div', 'li', 'p', 'pre', 'td', 'th'))
TAG_NAMES_HEADER = set(('h1', 'h2', 'h3', 'h4', 'h5', 'h6'))

BR_TO_P_STOP_TAGS = set(list(TAG_NAMES_BLOCK) + list(TAG_NAMES_HEADER) + ['br'])

_DEPTH_SCORE_DECAY = [(1 - d / 12.0) ** 5 for d in range(MAX_SCORE_DEPTH + 1)]

################################## DECORATORS ##################################

def DeferredRetryLimit(max_retries=3, failure_callback=None):
  """Catch and log all exceptions, but limit reraises to force retry."""
  def Decorator(func):
    @functools.wraps(func)
    def InnerDecorator(*args, **kwargs):
      # (catching Exception) pylint: disable-msg=W0702,W0703
      try:
        func(*args, **kwargs)
      except Exception, e1:
        try:
          retry_count = int(os.environ['HTTP_X_APPENGINE_TASKRETRYCOUNT'])
        except (KeyError, ValueError):
          logging.error('Could not determine task retry count.')
          return
        if retry_count < max_retries:
          # Reraise this error, so the task queue will rerun us.
          raise
        elif failure_callback:
          logging.error('Permanent failure, falling back; %s', e1)
          kwargs['exception'] = e1
          try:
            failure_callback(*args, **kwargs)
          except Exception, e2:
            logging.exception('failure_callback produced: %s', e2)
        else:
          logging.error('Permanent failure, no fallback')
    return InnerDecorator
  return Decorator


def Memoize(formatted_key, time=60*60):
  """Decorator to store a function call result in App Engine memcache."""

  def Decorator(func):
    @functools.wraps(func)
    def InnerDecorator(*args, **kwargs):
      key = formatted_key % args[0:formatted_key.count('%')]
      result = memcache.get_multi([key])
      if key in result:
        return result[key]
      result = func(*args, **kwargs)
      try:
        memcache.set(key, result, time)
      except ValueError:
        # Silently ignore ValueError which is probably too-long-value.
        pass
      return result
    return InnerDecorator
  return Decorator

################################### HELPERS ####################################

def ApplyScore(tag, score, depth=0, name=None):
  """Recursively apply a decaying score to each parent up the tree."""
  if not tag:
    return
  if depth > MAX_SCORE_DEPTH:
    return
  if tag.name == 'li' and score > 0:
    # Don't score list items positively.  Too likely to be false positives.
    return
  decayed_score = score * _DEPTH_SCORE_DECAY[depth]

  if not tag.has_key('score'): tag['score'] = 0.0
  tag['score'] += decayed_score

  if IS_DEV_APPSERVER and name:
    name_key = 'score_%s' % name
    if not tag.has_key(name_key):
      tag[name_key] = 0
    tag[name_key] = float(tag[name_key]) + decayed_score

  ApplyScore(tag.parent, score, depth + 1, name=name)


def CleanUrl(url):
  url = re.sub(r'utm_[a-z]+=[^&]+(&?)', r'\1', url)
  url = re.sub(r'[?&]+$', '', url)
  return url


@Memoize('Fetch_3_%s', 60 * 15)
def Fetch(orig_url):
  cookie = Cookie.SimpleCookie()
  redirect_limit = 10
  redirects = 0
  url = orig_url
  while url and redirects < redirect_limit:
    redirects += 1
    url = CleanUrl(url)
    if IS_DEV_APPSERVER:
      logging.info('Fetching: %s', url)
    final_url = url
    try:
      response = urlfetch.fetch(
          url, allow_truncated=True, follow_redirects=False, deadline=6,
          headers={'Cookie': cookie.output(attrs=(), header='', sep='; ')})
    except urlfetch.DownloadError, e:
      if 'ApplicationError: 2' in str(e) and '.nyud.net' not in url:
        new_url = list(urlparse.urlparse(url))
        new_url[1] = re.sub(r'$|:.*', '.nyud.net\g<0>', new_url[1])
        url = urlparse.urlunparse(new_url)
        continue
      raise
    cookie.load(response.headers.get('Set-Cookie', ''))
    previous_url = url
    url = response.headers.get('Location')
    if url:
      url = urlparse.urljoin(previous_url, url)
  final_url = urlparse.urljoin(orig_url, final_url)
  return (response, final_url)


def FindEmbeds(root_tag):
  if root_tag.name in EMBED_NAMES:
    yield root_tag
  for tag in root_tag.findAll(EMBED_NAMES):
    if tag.findParent(EMBED_NAMES):
      continue
    yield tag


def GetFeedEntryContent(entry):
  """Figure out the best content for this entry."""
  # Prefer "content".
  if 'content' in entry:
    # If there's only one, use it.
    if len(entry.content) == 1:
      return entry.content[0]['value']
    # Or, use the text/html type if there's more than one.
    for content in entry.content:
      if ('type' in content) and ('text/html' == content.type):
        return content['value']
  # Otherwise try "summary_detail" and "summary".
  if 'summary_detail' in entry:
    return entry.summary_detail['value']
  if 'summary' in entry:
    return entry.summary
  return ''


def ParseFeedAtUrl(url):
  """Fetch a URL's contents, and parse it as a feed."""
  response, _ = Fetch(url)
  try:
    feed_feedparser = feedparser.parse(response.content)
  except LookupError:
    return None
  else:
    return feed_feedparser


def PreCleanHtml(html):
  # Remove all HTML comments, doctypes.
  html = re.sub(RE_HTML_COMMENTS, '', html)
  html = re.sub(RE_DOCTYPE, '', html)
  html = html.replace('&nbsp;', ' ')

  return html


def RenderTemplate(template_name, template_values=None):
  template_values = template_values or {}
  template_file = os.path.join(
      os.path.dirname(__file__), 'templates', template_name)
  return template.render(template_file, template_values)


def SoupTagOnly(tag):
  return str(tag).split('>')[0] + '>'


def Strip(tag):
  # Switch this for dev.
  if 0:
    tag['style'] = 'outline: 2px dotted red'
  else:
    tag.extract()


def TagSize(tag):
  if tag.has_key('width') and tag.has_key('height'):
    w = tag['width']
    h = tag['height']
  elif tag.has_key('style'):
    try:
      w = re.search(r'width:\s*(\d+)px', tag['style']).group(1)
      h = re.search(r'height:\s*(\d+)px', tag['style']).group(1)
    except AttributeError:
      return None
  else:
    return None

  if w == '100%': w = 600
  if h == '100%': h = 400

  return w, h
