import os
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
import csv
from os import listdir
from os.path import isfile
import json
import re

STATE_FILE = "state.json"
TZ = timezone.utc
COUNCILS = ["1", "2"]  # Monitor both General Assembly (1) and Security Council (2)

def load_state():
    """Loads the script's state from the state file."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            try:
                # Structure: {'1': {'res_id': '...', 'last_ts': 12345}, '2': {...}}
                return json.load(f)
            except json.JSONDecodeError:
                return {c: {'res_id': None, 'last_ts': None, 'end_ts': None, 'res_name': None} for c in COUNCILS}
    return {c: {'res_id': None, 'last_ts': None, 'end_ts': None, 'res_name': None} for c in COUNCILS}

def save_state(state):
    """Saves the script's state to the state file."""
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=4)


def fetch_api_xml(council_id):
    """Fetches the raw XML from the NationStates API for a given council."""
    # NationStates requires a User-Agent header to identify the script.
    api_url = f"https://www.nationstates.net/cgi-bin/api.cgi?wa={council_id}&q=resolution+voters"
    headers = {'User-Agent': os.environ.get('USER_AGENT', 'WA voting recorder (Default UserAgent)')}
    response = requests.get(api_url, headers=headers)
    response.raise_for_status()
    return response.text


def fetch_happenings_page(sincetime, beforetime, limit=100, beforeid=None):
    """
    Fetches a single page of World Assembly voting events using the World API happenings shard.
    Returns the raw XML and the highest EVENT ID from the page (or None if empty).
    """
    api_url = (f"https://www.nationstates.net/cgi-bin/api.cgi?"
               f"q=happenings;filter=vote;sincetime={sincetime};beforetime={beforetime};limit={limit}")

    if beforeid:
        api_url += f";beforeid={beforeid}"

    headers = {'User-Agent': os.environ.get('USER_AGENT', 'WA voting recorder (Default UserAgent)')}
    response = requests.get(api_url, headers=headers)
    response.raise_for_status()

    root = ET.fromstring(response.text)
    events = root.findall('.//EVENT')

    next_before_id = None
    if events:
        # Events are returned newest (first) to oldest (last) when using 'beforeid'
        oldest_event = events[-1]
        try:
            oldest_id = int(oldest_event.get('id'))
            next_before_id = oldest_id
        except (ValueError, TypeError):
            # Should not happen
            pass

    return response.text, next_before_id


def backfill_missing_votes_via_happenings(council_id, res_id, res_name, last_ts, end_ts):
    """
    Fetches all vote-related happenings within the backfill window, then processes
    them in chronological order to ensure the final vote is correctly recorded.
    """
    print(f"WA {council_id} Resolution {res_id}: Backfilling votes from {last_ts} to {end_ts}...")

    # Fetch ALL missing vote happenings (newest to oldest, as API provides)
    all_events = []
    next_before_id = None
    limit = 100
    page_count = 0

    while True:
        page_count += 1
        print(f"Fetching happenings page {page_count}...")

        try:
            happenings_xml, next_before_id_for_next_page = fetch_happenings_page(
                sincetime=last_ts,
                beforetime=end_ts,
                limit=limit,
                beforeid=next_before_id
            )
        except requests.HTTPError as e:
            print(f"Error fetching happenings page {page_count}: {e}. Stopping backfill.")
            break

        happenings_root = ET.fromstring(happenings_xml)
        events_on_page = happenings_root.findall('.//EVENT')

        if not events_on_page:
            print("Finished fetching all happening events.")
            break

        all_events.extend(events_on_page)

        # If the page returned less than the limit, we have reached the end of the events.
        if len(events_on_page) < limit:
            print(
                f"Fetched {len(events_on_page)} events, which is less than the limit of {limit}. Stopping pagination.")
            break

        # If page returned limit, get the next ID for the next page fetch
        next_before_id = next_before_id_for_next_page
        if next_before_id is None:
            # If NS is trolling us
            print("Received full page but no next_before_id. Stopping pagination.")
            break

    if not all_events:
        print(f"WA {council_id} Resolution {res_id}: No new votes found in happenings.")
        return

    # Process events in CHRONOLOGICAL order (OLDEST to NEWEST)
    # The list is currently newest to oldest. Sorting by TIMESTAMP (oldest first)
    # We need to apply the oldest vote first so subsequent changes overwrite it.

    # Note: Event ID is a good secondary sort, as higher IDs are newer events.
    parsed_events = []

    vote_pattern = re.compile(
        r"@@(?P<nation>[a-zA-Z0-9_]+)@@ voted (?P<type>for|against) the World Assembly Resolution \"(?P<resname>.+?)\"\.")
    withdraw_pattern = re.compile(
        r"@@(?P<nation>[a-zA-Z0-9_]+)@@ withdrew its vote on the World Assembly Resolution \"(?P<resname>.+?)\"\.")

    for event in all_events:
        try:
            text = event.find('TEXT').text
            timestamp = int(event.find('TIMESTAMP').text)
            event_id = int(event.get('id'))
        except (ValueError, TypeError, AttributeError):
            continue  # should not happen

        match = vote_pattern.search(text)
        if match and match.group('resname') == res_name:
            nation_id = match.group('nation')
            vote_type = match.group('type')
            parsed_events.append((timestamp, event_id, nation_id, vote_type))
            continue

        match = withdraw_pattern.search(text)
        if match and match.group('resname') == res_name:
            nation_id = match.group('nation')
            # 'withdraw' will clear the vote in the final step
            parsed_events.append((timestamp, event_id, nation_id, 'withdraw'))

    # Sort by timestamp (primary) and event ID (secondary) to get chronological order
    parsed_events.sort(key=lambda x: (x[0], x[1]))

    new_votes = {}

    for timestamp, event_id, nation_id, vote_type in parsed_events:
        # Later (newer) events will overwrite earlier ones for the same nation_id
        new_votes[nation_id] = vote_type

    filename = f"resolutions/{res_id}_votes.xml"
    if not os.path.exists(filename):
        print(f"Error: Base file {filename} not found for backfill.")
        return

    with open(filename, 'r') as f:
        raw_xml = f.read()
        root = ET.fromstring(raw_xml)

    resolution_tag = root.find('RESOLUTION')
    if resolution_tag is None:
        print(f"Error: RESOLUTION tag missing in base file {filename}.")
        return

    final_votes = {}

    # Collect votes from the existing XML
    votes_for_tag = resolution_tag.find('VOTES_FOR')
    votes_against_tag = resolution_tag.find('VOTES_AGAINST')

    if votes_for_tag:
        for vote in votes_for_tag.findall('N'):
            final_votes[vote.text] = 'for'
    if votes_against_tag:
        for vote in votes_against_tag.findall('N'):
            final_votes[vote.text] = 'against'

    for nation_id, vote_type in new_votes.items():
        if vote_type == 'withdraw':
            final_votes.pop(nation_id, None)
        else:
            final_votes[nation_id] = vote_type

    if votes_for_tag is not None:
        votes_for_tag.clear()
    else:
        votes_for_tag = ET.SubElement(resolution_tag, 'VOTES_FOR')

    if votes_against_tag is not None:
        votes_against_tag.clear()
    else:
        votes_against_tag = ET.SubElement(resolution_tag, 'VOTES_AGAINST')

    for nation_id, vote_type in final_votes.items():
        if vote_type == 'for':
            ET.SubElement(votes_for_tag, 'N').text = nation_id
        elif vote_type == 'against':
            ET.SubElement(votes_against_tag, 'N').text = nation_id

    final_xml_string = ET.tostring(root, encoding='utf-8').decode('utf-8')

    with open(filename, 'w') as f:
        f.write(final_xml_string)

    os.system('git config user.name "GitHub Actions Bot"')
    os.system('git config user.email "github-actions-bot@users.noreply.github.com"')
    os.system(f'git add {filename}')
    os.system(f'git commit -m "FINALIZE: Final vote record for resolution {res_id}"')
    print(f"WA {council_id} Resolution {res_id}: Final vote record saved and committed.")


def process_execution_request():
    """
    Called hourly to check if any resolution is active, save its vote record,
    and trigger backfilling when a resolution ends.
    """
    current_timestamp = int(datetime.now(tz=TZ).timestamp())
    script_state = load_state()

    for council_id in COUNCILS:
        print(f"\n--- Processing WA {council_id} ---")
        current_state = script_state.get(council_id,
                                         {'res_id': None, 'last_ts': None, 'end_ts': None, 'res_name': None})

        try:
            raw_xml = fetch_api_xml(council_id)
            root = ET.fromstring(raw_xml)
            resolution_tag = root.find('RESOLUTION')

            # No active resolution
            if resolution_tag is None or resolution_tag.find('ID') is None:
                print(f"Council {council_id}: No resolution currently at vote.")

                # Check if a resolution just ended
                if current_state['res_id'] is not None and current_state['last_ts'] is not None:
                    # Trigger backfill using the last state data.
                    res_id = current_state['res_id']
                    last_ts = current_state['last_ts']
                    end_ts = current_state['end_ts']
                    res_name = current_state['res_name']

                    print(f"WA {council_id}: Detected end of Resolution {res_id}. Triggering backfill.")

                    backfill_missing_votes_via_happenings(council_id, res_id, res_name, last_ts, end_ts)

                    current_state['res_id'] = None
                    current_state['res_name'] = None
                    current_state['last_ts'] = None
                    current_state['end_ts'] = None

            # Active resolution
            else:
                resolution_id = resolution_tag.find('ID').text
                resolution_name = resolution_tag.find('NAME').text

                # Check if resolution was switched
                if current_state['res_id'] is not None and current_state['res_id'] != resolution_id:
                    print(f"WA {council_id}: Resolution {current_state['res_id']} replaced by {resolution_id}.")
                    backfill_missing_votes_via_happenings(
                        council_id,
                        current_state['res_id'],
                        current_state['res_name'],
                        current_state['last_ts'],
                        current_state['end_ts']
                    )

                filename = f"resolutions/{resolution_id}_votes.xml"
                promoted_ts = int(resolution_tag.find('PROMOTED').text)

                # Resolution voting periods last exactly 4 days (345600 seconds).
                TIME_TO_VOTE_END_SECONDS = 345600
                voting_end_timestamp = promoted_ts + TIME_TO_VOTE_END_SECONDS

                with open(filename, 'w') as f:
                    f.write(raw_xml)

                os.system('git config user.name "GitHub Actions Bot"')
                os.system('git config user.email "github-actions-bot@users.noreply.github.com"')
                os.system(f'git add {filename}')
                # Use || true in case the file hasn't changed
                os.system(f'git commit -m "UPDATE: Hourly vote record for resolution {resolution_id}" || true')

                # Update state
                current_state['res_id'] = resolution_id
                current_state['res_name'] = resolution_name
                current_state['last_ts'] = current_timestamp
                current_state['end_ts'] = voting_end_timestamp

                remaining_seconds = voting_end_timestamp - current_timestamp
                remaining_time = str(timedelta(seconds=remaining_seconds))
                end_dt = datetime.fromtimestamp(voting_end_timestamp, tz=TZ).strftime('%Y-%m-%d %H:%M:%S UTC')
                print(f"WA {council_id}: Successfully saved {filename}. {remaining_time} remaining. End: {end_dt}")

            script_state[council_id] = current_state

        except requests.HTTPError as e:
            print(f"Error fetching API data for WA {council_id}: {e}")
        except Exception as e:
            print(f"Error during execution/save for WA {council_id}: {e}")

    save_state(script_state)

def csv_vote_record():
    res_files = listdir('resolutions')
    res_files.sort(key=lambda x: int(x.split('_')[1]))
    all_votes = {}
    all_res = []
    columns = ['nation_id']
    for file in res_files:
        path = 'resolutions/' + file
        if isfile(path):
            with open(path, 'r') as f:
                root = ET.fromstring(f.read())
                resolution = root.find('RESOLUTION')
                resolution_id = resolution.find('ID').text
                columns.append(resolution_id)
                if resolution.find('COAUTHOR') is not None:
                    coauthors = ','.join([x.text for x in resolution.find('COAUTHOR').findall('N')])
                else:
                    coauthors = ''
                resolution_info = {
                    'id': resolution_id,
                    'council': root.get('council'),
                    'name': resolution.find('NAME').text,
                    'proposed_by': resolution.find('PROPOSED_BY').text,
                    'promoted': resolution.find('PROMOTED').text,
                    'coauthor': coauthors,
                }
                all_res.append(resolution_info)
                for vote in resolution.find('VOTES_FOR').findall('N'):
                    nation_id = vote.text
                    if nation_id not in all_votes:
                        all_votes[nation_id] = {'nation_id': nation_id, resolution_id: 1}
                    else:
                        all_votes[nation_id][resolution_id] = 1
                for vote in resolution.find('VOTES_AGAINST').findall('N'):
                    nation_id = vote.text
                    if nation_id not in all_votes:
                        all_votes[nation_id] = {'nation_id': nation_id, resolution_id: 0}
                    else:
                        all_votes[nation_id][resolution_id] = 0
    with open('resolutions.csv', 'w', newline='') as resfile:
        res_columns = ['id', 'council', 'name', 'proposed_by', 'promoted', 'coauthor']
        writer = csv.DictWriter(resfile, fieldnames=res_columns)
        writer.writeheader()
        for res in all_res:
            writer.writerow(res)
    with open('votes.csv', 'w', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=columns)
        writer.writeheader()
        for nation_id, votes in all_votes.items():
            writer.writerow(votes)

    os.system('git config user.name "GitHub Actions Bot"')
    os.system('git config user.email "github-actions-bot@users.noreply.github.com"')
    os.system(f'git add votes.csv resolutions.csv {STATE_FILE}')
    # Use || true in case the file hasn't changed since the last hour.
    os.system(f'git commit -m "UPDATE: Vote record CSV" || true')
    os.system('git push')


if __name__ == "__main__":
    mode = os.environ.get("MONITOR_MODE")
    if mode == "EXECUTE":
        process_execution_request()
        csv_vote_record()
    else:
        print("Error: MONITOR_MODE environment variable is not set correctly.")