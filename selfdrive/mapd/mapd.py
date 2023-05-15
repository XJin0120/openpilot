#!/usr/bin/env python3
import threading
from traceback import print_exception
import numpy as np
import cereal.messaging as messaging
from common.realtime import Ratekeeper, set_core_affinity, set_realtime_priority
from selfdrive.mapd.lib.osm import OSM
from selfdrive.mapd.lib.geo import distance_to_points
from selfdrive.mapd.lib.WayCollection import WayCollection
from selfdrive.mapd.config import QUERY_RADIUS, MIN_DISTANCE_FOR_NEW_QUERY, FULL_STOP_MAX_SPEED, LOOK_AHEAD_HORIZON_TIME
from system.swaglog import cloudlog
from cereal import log
from selfdrive.navd.helpers import Coordinate
import math

_DEBUG = False
_CLOUDLOG_DEBUG = True


def _debug(msg, log_to_cloud=True):
  if _CLOUDLOG_DEBUG and log_to_cloud:
    cloudlog.debug(msg)
  if _DEBUG:
    print(msg)


def excepthook(args):
  _debug(f'MapD: Threading exception:\n{args}')
  print_exception(args.exc_type, args.exc_value, args.exc_traceback)


threading.excepthook = excepthook


class MapD():
  def __init__(self):
    self.osm = OSM()
    self.way_collection = None
    self.route = None
    self.last_gps_fix_timestamp = 0
    self.last_gps = None
    self.location_deg = None  # The current location in degrees.
    self.location_rad = None  # The current location in radians as a Numpy array.
    self.bearing_rad = None
    self.location_stdev = None  # The current location accuracy in mts. 1 standard devitation.
    self.gps_speed = 0.
    self.last_fetch_location = None
    self.last_route_update_fix_timestamp = 0
    self.last_publish_fix_timestamp = 0
    self._query_thread = None
    self._lock = threading.RLock()

  def update_location(self, sm):
    sock = 'liveLocationKalman'
    if not sm.updated[sock] or not sm.valid[sock]:
      return

    location = sm[sock]

    locationd_valid = (location.status == log.LiveLocationKalman.Status.valid) and location.positionGeodetic.valid
    if not locationd_valid:
      return

    self.last_gps_fix_timestamp = location.unixTimestampMillis  # Unix TS. Milliseconds since January 1, 1970.
    position = Coordinate(location.positionGeodetic.value[0], location.positionGeodetic.value[1])
    self.location_rad = np.radians(np.array([position.latitude, position.longitude], dtype=float))
    self.location_deg = (position.latitude, position.longitude)
    self.bearing_rad = np.radians(math.degrees(location.calibratedOrientationNED.value[2]), dtype=float)
    self.gps_speed = location.velocityCalibrated.value[0]
    self.location_stdev = 1.

  def _query_osm_not_blocking(self):
    def query(osm, location_deg, location_rad, radius):
      _debug(f'Mapd: Start query for OSM map data at {location_deg}')
      lat, lon = location_deg
      ways = osm.fetch_road_ways_around_location(lat, lon, radius)
      _debug(f'Mapd: Query to OSM finished with {len(ways)} ways')

      # Only issue an update if we received some ways. Otherwise it is most likely a conectivity issue.
      # Will retry on next loop.
      if len(ways) > 0:
        new_way_collection = WayCollection(ways, location_rad)

        # Use the lock to update the way_collection as it might be being used to update the route.
        _debug('Mapd: Locking to write results from osm.', log_to_cloud=False)
        with self._lock:
          self.way_collection = new_way_collection
          self.last_fetch_location = location_rad
          _debug(f'Mapd: Updated map data @ {location_deg} - got {len(ways)} ways')

        _debug('Mapd: Releasing Lock to write results from osm', log_to_cloud=False)

    # Ignore if we have a query thread already running.
    if self._query_thread is not None and self._query_thread.is_alive():
      return

    self._query_thread = threading.Thread(target=query, args=(self.osm, self.location_deg, self.location_rad,
                                                              QUERY_RADIUS))
    self._query_thread.start()

  def updated_osm_data(self):
    if self.route is not None:
      distance_to_end = self.route.distance_to_end
      if distance_to_end is not None and distance_to_end >= MIN_DISTANCE_FOR_NEW_QUERY:
        # do not query as long as we have a route with enough distance ahead.
        return

    if self.location_rad is None:
      return

    if self.last_fetch_location is not None:
      distance_since_last = distance_to_points(self.last_fetch_location, np.array([self.location_rad]))[0]
      if distance_since_last < QUERY_RADIUS - MIN_DISTANCE_FOR_NEW_QUERY:
        # do not query if are still not close to the border of previous query area
        return

    self._query_osm_not_blocking()

  def update_route(self):
    def update_proc():
      # Ensure we clear the route on op disengage, this way we can correct possible incorrect map data due
      # to wrongly locating or picking up the wrong route.
      # if self._disengaging:
      #   self.route = None
      #   _debug('Mapd *****: Clearing Route as system is disengaging. ********')

      if self.way_collection is None or self.location_rad is None or self.bearing_rad is None:
        _debug('Mapd *****: Can not update route. Missing WayCollection, location or bearing ********')
        return

      if self.route is not None and self.last_route_update_fix_timestamp == self.last_gps_fix_timestamp:
        _debug('Mapd *****: Skipping route update. No new fix since last update ********')
        return

      self.last_route_update_fix_timestamp = self.last_gps_fix_timestamp

      # Create the route if not existent or if it was generated by an older way collection
      if self.route is None or self.route.way_collection_id != self.way_collection.id:
        self.route = self.way_collection.get_route(self.location_rad, self.bearing_rad, self.location_stdev)
        _debug(f'Mapd *****: Route created: \n{self.route}\n********')
        return

      # Do not attempt to update the route if the car is going close to a full stop, as the bearing can start
      # jumping and creating unnecesary loosing of the route. Since the route update timestamp has been updated
      # a new liveMapData message will be published with the current values (which is desirable)
      if self.gps_speed < FULL_STOP_MAX_SPEED:
        _debug('Mapd *****: Route Not updated as car has Stopped ********')
        return

      self.route.update(self.location_rad, self.bearing_rad, self.location_stdev)
      if self.route.located:
        _debug(f'Mapd *****: Route updated: \n{self.route}\n********')
        return

      # if an old route did not mange to locate, attempt to regenerate form way collection.
      self.route = self.way_collection.get_route(self.location_rad, self.bearing_rad, self.location_stdev)
      _debug(f'Mapd *****: Failed to update location in route. Regenerated with route: \n{self.route}\n********')

    # We use the lock when updating the route, as it reads `way_collection` which can ben updated by
    # a new query result from the _query_thread.
    _debug('Mapd: Locking to update route.', log_to_cloud=False)
    with self._lock:
      update_proc()

    _debug('Mapd: Releasing Lock to update route', log_to_cloud=False)

  def publish(self, pm, sm):
    # Ensure we have a route currently located
    if self.route is None or not self.route.located:
      _debug('Mapd: Skipping liveMapData message as there is no route or is not located.')
      return

    # Ensure we have a route update since last publish
    if self.last_publish_fix_timestamp == self.last_route_update_fix_timestamp:
      _debug('Mapd: Skipping liveMapData since there is no new gps fix.')
      return

    self.last_publish_fix_timestamp = self.last_route_update_fix_timestamp

    speed_limit = self.route.current_speed_limit
    next_speed_limit_section = self.route.next_speed_limit_section
    current_road_name = self.route.current_road_name

    map_data_msg = messaging.new_message('liveMapData')
    map_data_msg.valid = sm.all_alive(service_list=['liveLocationKalman']) and \
                         sm.all_valid(service_list=['liveLocationKalman'])

    map_data_msg.liveMapData.speedLimitValid = bool(speed_limit is not None)
    map_data_msg.liveMapData.speedLimit = float(speed_limit if speed_limit is not None else 0.0)
    map_data_msg.liveMapData.speedLimitAheadValid = bool(next_speed_limit_section is not None)
    map_data_msg.liveMapData.speedLimitAhead = float(next_speed_limit_section.value
                                                     if next_speed_limit_section is not None else 0.0)
    map_data_msg.liveMapData.speedLimitAheadDistance = float(next_speed_limit_section.start
                                                             if next_speed_limit_section is not None else 0.0)

    map_data_msg.liveMapData.currentRoadName = str(current_road_name if current_road_name is not None else "")

    pm.send('liveMapData', map_data_msg)
    _debug(f'Mapd *****: Publish: \n{map_data_msg}\n********', log_to_cloud=False)


# provides live map data information
def mapd_thread(sm=None, pm=None):
  set_core_affinity([1,])
  set_realtime_priority(1)
  mapd = MapD()
  rk = Ratekeeper(1., print_delay_threshold=None)  # Keeps rate at 1 hz

  # *** setup messaging
  if sm is None:
    sm = messaging.SubMaster(['liveLocationKalman'])
  if pm is None:
    pm = messaging.PubMaster(['liveMapData'])

  while True:
    sm.update()
    mapd.update_location(sm)
    mapd.updated_osm_data()
    mapd.update_route()
    mapd.publish(pm, sm)
    rk.keep_time()


def main(sm=None, pm=None):
  mapd_thread(sm, pm)


if __name__ == "__main__":
  main()
