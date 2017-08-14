#!/usr/bin/env python3

import itertools
import os
import sys
import time
import re
import json

import attr
import backoff
import pendulum
import requests
import dateutil.parser
import singer
import singer.metrics as metrics
from singer import utils
from singer import (UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING,
                    _transform_datetime)


class SourceUnavailableException(Exception):
    '''Exception for source unavailable'''
    pass


REQUIRED_CONFIG_KEYS = ["api_key", "start_date"]
PER_PAGE = 100
BASE_URL = "https://api2.autopilothq.com/v1"
CONFIG = {
    "api_token": None,
    "start_date": None,
    "user_agent": None
}


LOGGER = singer.get_logger()
SESSION = requests.session()


ENDPOINTS = {
    "contacts":                "/contacts",
    "custom_fields":           "/contacts/custom_fields",
    "lists":                   "/lists",
    "smart_segments":          "/smart_segments",
    "smart_segments_contacts": "/smart_segments/{segment_id}/contacts",
}


def get_abs_path(path):
    '''Returns the absolute path'''
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), path)


def get_field_type(field_type):
    '''Map the field type from Autopilot to Singer Spec'''
    if field_type == "boolean":
        return {"type": ["null", "boolean"]}

    elif field_type == "date":
       return {"type": ["null", "string"], "format": "date-time"}

    elif field_type == "integer":
        return {"type": ["null", "integer"]}

    elif field_type == "float" or field_type == "number":
        return {"type": ["null", "number"]}

    else:
        return {"type": ["null", "string"]}


def parse_custom_schema(JSON):
    '''Parse the custom schema returned from Autopilot and format
    it into JSON schema format
    
    Example Payload:
    [
        {
            "fieldType": "string",
            "key": "contacts_customfields_1456200756325_ab876950-d9e3-11e5-b21c-31af5a6619a2",
            "name": "visitedCities"
        },
        {
            "fieldType": "string",
            "key": "contacts_customfields_1456200763929_b00fb090-d9e3-11e5-b21c-31af5a6619a2",
            "name": "visitedCountries"
        }
    ]
    '''
    parsed_schema = []
    for custom_field in JSON:
        parsed_schema.append({
            custom_field["name"]: get_field_type(custom_field["fieldType"])
        })

    return parsed_schema


def load_custom_schema():
    '''Returns the contacts schema with any custom fields appended'''
    return parse_custom_schema(request(get_url("custom_fields")).json())


def load_schema(entity):
    '''Returns the schema for the specified source
    Contacts need to have the custom fields appended'''
    schema = utils.load_json(get_abs_path("schemas/{}.json".format(entity)))
    
    if entity is 'contacts':
        custom_fields = load_custom_schema()
        schema['properties']['custom'] = {
            "type": ["null", "array"],
            "items": custom_fields
        }

    return schema


def get_start(STATE):
    if "currently_syncing" in STATE:
        currentSource = STATE["currently_syncing"]
        if "bookmarks" in STATE:
            bookmarks = STATE["bookmarks"]
            if "updated_at" in bookmarks[currentSource]:
                return bookmarks[currentSource]["updated_at"]
    
    if "start_date" not in CONFIG:
        return None

    return CONFIG["start_date"]


def client_error(exc):
    '''Indicates whether the given RequestException is a 4xx response'''
    return exc.response is not None and 400 <= exc.response.status_code < 500


def parse_source_from_url(url):
    '''Given an Autopilot URL, extract the source name (e.g. "contacts")'''
    url_regex = re.compile(BASE_URL +  r'.*/(\w+)')
    match = url_regex.match(url)

    if match:
        if match.group(1) == "contacts":
            if "segment" in match.group(0):
                return "smart_segments_contacts"
        return match.group(1)

    raise ValueError("Can't determine stream from URL " + url)


def parse_key_from_source(source):
    '''Given an Autopilot source, return the key needed to access the children
       The endpoints for fetching contacts related to a list or segment
       have the contacts in a child with the key of contacts
    '''
    if 'contact' in source:
        return 'contacts'

    elif 'smart_segments' in source:
        return 'segments'

    return source


def convert_to_snake(name):
    '''Convert CamelCase keys to snake_case'''
    snake_one = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", snake_one).lower()


def transform_contact(contact):
    '''Transform the properties on a contact
    to be more database friendly

    TODO: Figure out the best way to handle custom fields
    '''
    boolean_props = ["anywhere_page_visits", "anywhere_form_submits", "anywhere_utm"]
    timestamp_props = ["mail_received", "mail_opened", "mail_clicked", "mail_bounced", "mail_complained", "mail_unsubscribed", "mail_hardbounced"]

    for prop in boolean_props:
        if prop in contact:
            formatted_array = []
            for row in contact[prop]:
                formatted_array.append({
                    "url": row,
                    "value": contact[prop][row]
                })
            contact[prop] = formatted_array

    for prop in timestamp_props:
        if prop in contact:
            formatted_array = []
            for row in contact[prop]:
                formatted_array.append({
                    "id": row,
                    "timestamp": _transform_datetime(
                        (contact[prop][row]),
                        UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING)
                })
            contact[prop] = formatted_array

    if "custom_fields" in contact:
        new_custom_fields = []
        custom_fields = contact["custom_fields"]
        for row in custom_fields:
            new_custom_fields.append({
                row["kind"]: row["value"]
            })
        contact["custom_fields"] = new_custom_fields

    return contact


def get_url(endpoint, **kwargs):
    '''Get the full url for the endpoint'''
    if endpoint not in ENDPOINTS:
        raise ValueError("Invalid endpoint {}".format(endpoint))
    

    return BASE_URL + ENDPOINTS[endpoint].format(**kwargs)


@backoff.on_exception(backoff.expo,
                      (requests.exceptions.RequestException),
                      max_tries=5,
                      giveup=client_error,
                      factor=2)
@utils.ratelimit(20, 1)
def request(url, params=None):
    '''Make a request to the given Autopilot URL.
    Appends Autopilot API bookmark to url if in params
    
    Handles retrying, rate-limiting and status checking. 
    Logs request duration and records per second
    '''
    headers = {"autopilotapikey": CONFIG["api_key"]}

    if "user_agent" in CONFIG and CONFIG["user_agent"] is not None:
        headers["user-agent"] = CONFIG["user_agent"]

    if params and "bookmark" in params:
        url = url + "/" + params["bookmark"]

    req = requests.Request("GET", url, headers=headers).prepare()
    LOGGER.info("GET %s", req.url)

    with metrics.http_request_timer(parse_source_from_url(url)) as timer:
        resp = SESSION.send(req)
        timer.tags[metrics.Tag.http_status_code] = resp.status_code
        resp.raise_for_status()
        return resp


def gen_request(STATE, endpoint, params=None):
    '''Generate a request that will iterate through the results
    and paginate through the responses until the amount of results
    returned is less than 100, the amount returned by the API.

    If the source has 'contact' in it, Autopilot API will provide a
    'bookmark' property at the top level that is used to paginate
    results



    The API only returns bookmarks for iterating through contacts
    '''
    params = params or {}

    source = parse_source_from_url(endpoint)
    source_key = parse_key_from_source(source)

    with metrics.record_counter(source) as counter:
        while True:
            data = request(endpoint, params).json()
            if 'contact' in source:
                if "bookmark" in data:
                    params["bookmark"] = data["bookmark"]

                else:
                    params = {}

            for row in data[source_key]:
                counter.increment()
                yield row
            
            if len(data[source_key]) < PER_PAGE:
                params = {}
                break


def sync_contacts(STATE, catalog):
    '''Sync contacts from the Autopilot API

    The API returns data in the following format

    {
        "contacts": [{...},{...}],
        "total_contacts": 400,
        "bookmark": "person_9EAF39E4-9AEC-4134-964A-D9D8D54162E7"
    }
    '''
    schema = load_schema("contacts")
    singer.write_schema("contacts", schema, ["contact_id"], catalog.get("stream_alias"))

    params = {}
    most_recent_updated_time = get_start(STATE)

    for row in gen_request(STATE, get_url("contacts"), params):
        if "updated_at" in row and most_recent_updated_time < row["updated_at"]:
            singer.write_record("contacts", transform_contact(row))
            most_recent_updated_time = row["updated_at"]

    STATE = singer.write_bookmark(STATE, 'contacts', 'updated_at', most_recent_updated_time) 
    singer.write_state(STATE)

    LOGGER.info("Completed Contacts Sync")
    return STATE


def sync_lists(STATE, catalog):
    '''Sync all lists from the Autopilot API

    The API returns data in the following format

    {
        "lists": [
            {
            "list_id": "contactlist_06444749-9C0F-4894-9A23-D6872F9B6EF8",
            "title": "1k.csv"
            },
            {
            "list_id": "contactlist_0FBA1FA2-5A12-413B-B1A8-D113E6B3CDA8",
            "title": "____NEW____"
            }
        ]
     }

    '''
    schema = load_schema("lists")
    singer.write_schema("lists", schema, ["list_id"], catalog.get("stream_alias"))

    for row in gen_request(STATE, get_url("lists")):
        singer.write_record("lists", row)
        utils.update_state(STATE, "lists", row["list_id"])

    singer.write_state(STATE)
    LOGGER.info("Completed Lists Sync")
    return STATE


def sync_smart_segments(STATE, catalog):
    '''Sync all smart segments from the Autopilot API

    The API returns data in the following format

    {
        "segments": [
            {
            "segment_id": "contactlist_sseg1456891025207",
            "title": "Ladies"
            },
            {
            "segment_id": "contactlist_sseg1457059448884",
            "title": "Gentlemen"
            }
        ]
    }

    '''
    schema = load_schema("smart_segments")
    singer.write_schema("smart_segments", schema, ["segment_id"], catalog.get("stream_alias"))
    params = {}

    for row in gen_request(STATE, get_url("smart_segments"), params):
        singer.write_record("smart_segments", row)
        utils.update_state(STATE, "smart_segments", row["segment_id"])

    singer.write_state(STATE)
    LOGGER.info("Completed Smart Segments Sync")
    return STATE


def sync_smart_segment_contacts(STATE, catalog):
    '''Sync the contacts on a given smart segment from the Autopilot API

    {
        "contacts": [{...},{...}],
        "total_contacts": 2
    }
    '''
    schema = load_schema("smart_segments_contacts")
    singer.write_schema(
        "smart_segments_contacts",
        schema,
        ["segment_id", "contact_id"],
        catalog.get("stream_alias"))
    params = {}

    for row in gen_request(STATE, get_url("smart_segments"), params):
        subrow_url = get_url("smart_segments_contacts", segment_id=row["segment_id"])
        for subrow in gen_request(STATE, subrow_url, params):
            singer.write_record("smart_segments_contacts", {
                "segment_id": row["segment_id"],
                "contact_id": subrow["contact_id"]
            })

        utils.update_state(STATE, "smart_segments_contacts", row["segment_id"])
        LOGGER.info("Completed Smart Segment's Contacts Sync")

    singer.write_state(STATE)
    LOGGER.info("Completed Smart Segments Contacts Sync")
    return STATE


@attr.s
class Stream(object):
    tap_stream_id = attr.ib()
    sync = attr.ib()

STREAMS = [
    Stream("contacts", sync_contacts),
    Stream("lists", sync_lists),
    Stream("smart_segments", sync_smart_segments),
    Stream("smart_segments_contacts", sync_smart_segment_contacts)
]


def get_streams_to_sync(streams, state):
    '''Get the streams to sync'''
    current_stream = singer.get_currently_syncing(state)
    result = streams
    if current_stream:
        result = list(itertools.dropwhile(
            lambda x: x.tap_stream_id != current_stream, streams))
    if not result:
        raise Exception("Unknown stream {} in state".format(current_stream))
    return result


def get_selected_streams(remaining_streams, annotated_schema):
    selected_streams = []

    for stream in remaining_streams:
        tap_stream_id = stream.tap_stream_id
        for annotated_stream in annotated_schema["streams"]:
            if tap_stream_id == annotated_stream["tap_stream_id"]:
                schema = annotated_stream["schema"]
                if "selected" in schema and schema["selected"] is True:
                    selected_streams.append(stream)

    return selected_streams


def do_sync(STATE, catalogs):
    '''Do a full sync'''
    remaining_streams = get_streams_to_sync(STREAMS, STATE)
    selected_streams = get_selected_streams(remaining_streams, catalogs)
    
    if len(selected_streams) < 1:
        LOGGER.info("No Streams selected, please check that you have a schema selected in your catalog")
        return

    LOGGER.info("Starting sync. Will sync these streams: %s",
                [stream.tap_stream_id for stream in selected_streams])

    for stream in selected_streams:
        LOGGER.info("Syncing %s", stream.tap_stream_id)
        utils.update_state(STATE, "currently_syncing", stream.tap_stream_id)
        singer.write_state(STATE)

        try:
            catalog = [c for c in catalogs.get('streams')
                       if c.get('stream') == stream.tap_stream_id][0]
            STATE = stream.sync(STATE, catalog)
        except SourceUnavailableException:
            pass

    utils.update_state(STATE, "currently_syncing", None)
    singer.write_state(STATE)
    LOGGER.info("Sync completed")


def load_discovered_schema(stream):
    schema = load_schema(stream.tap_stream_id)
    for k in schema['properties']:
        schema['properties'][k]['inclusion'] = 'automatic'
    return schema


def discover_schemas():
    result = {'streams': []}
    for stream in STREAMS:
        LOGGER.info('Loading schema for %s', stream.tap_stream_id)
        result['streams'].append({'stream': stream.tap_stream_id,
                                  'tap_stream_id': stream.tap_stream_id,
                                  'schema': load_discovered_schema(stream)})
    return result


def do_discover():
    LOGGER.info("Loading Schemas")
    json.dump(discover_schemas(), sys.stdout, indent=4)


def main():
    '''Entry point'''
    args = utils.parse_args(REQUIRED_CONFIG_KEYS)

    CONFIG.update(args.config)
    STATE = {}

    if args.state:
        STATE.update(args.state)

    if args.discover:
        do_discover()
    elif args.catalog:
        do_sync(STATE, args.catalog.to_dict())
    else:
        LOGGER.info("No Streams were selected")


if __name__ == "__main__":
    main()
