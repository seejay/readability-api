#!/usr/bin/env python
"""Views for scheduled tasks for Readability API project.

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

import datetime
import logging

from google.appengine.ext import db
from google.appengine.ext import deferred
from google.appengine.ext import webapp
from google.appengine.ext.webapp.util import run_wsgi_app

import feed
import models
import util

_MIN_UPDATE_INTERVAL = datetime.timedelta(days=1)


class CleanStaleEntries(webapp.RequestHandler):
  request = None
  response = None

  def get(self):
    for feed_entity in models.Feed.all():
      db.delete(feed_entity.stale_entries)


class UpdateFeeds(webapp.RequestHandler):
  request = None
  response = None

  def get(self):
    for feed_entity in models.Feed.all().order('last_fetch_time'):
      # Figure the average interval between updates.
      entries = feed_entity.entries
      do_update = True
      if entries:
        interval = (entries[0].created - entries[-1].created) / len(entries)
        interval = min(_MIN_UPDATE_INTERVAL, interval)
        # If the newest update + the update interval > now, skip updating.
        if datetime.datetime.now() < entries[0].created + interval:
          do_update = False
      if do_update:
        # Update this feed!
        deferred.defer(feed.UpdateFeed, feed_entity.key(), _queue='update')


def main():
  application = webapp.WSGIApplication(
      [('/crons/clean_stale_entries', CleanStaleEntries),
       ('/crons/update_feeds', UpdateFeeds),
      ],
      debug=util.IS_DEV_APPSERVER)
  run_wsgi_app(application)


if __name__ == '__main__':
  main()
