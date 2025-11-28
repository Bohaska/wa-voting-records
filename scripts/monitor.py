import os
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
import csv
from os import listdir
from os.path import isfile

STATE_FILE = "state.json"
TZ = timezone.utc
COUNCILS = ["1", "2"]  # Monitor both General Assembly (1) and Security Council (2)


def fetch_api_xml(council_id):
    """Fetches the raw XML from the NationStates API for a given council."""
    # NationStates requires a User-Agent header to identify the script.
    api_url = f"https://www.nationstates.net/cgi-bin/api.cgi?wa={council_id}&q=resolution+voters"
    headers = {'User-Agent': os.environ.get('USER_AGENT', 'WA voting recorder (Default UserAgent)')}
    response = requests.get(api_url, headers=headers)
    response.raise_for_status()
    return response.text


def process_execution_request():
    """
    Called hourly to check if any resolution is active, save its vote record,
    and reset monitoring once the voting has ended.
    """
    current_timestamp = int(datetime.now(tz=TZ).timestamp())

    for council_id in COUNCILS:
        print(f"WA {council_id}: Overwriting hourly vote record...")

        try:
            raw_xml = fetch_api_xml(council_id)
            root = ET.fromstring(raw_xml)

            resolution_tag = root.find('RESOLUTION')
            # Handle empty tag case when no resolution is at vote.
            if resolution_tag is None or resolution_tag.find('ID') is None:
                print(f"Council {council_id}: No resolution currently at vote.")
                continue

            resolution_id = resolution_tag.find('ID').text
            filename = f"resolutions/{resolution_id}_votes.xml"

            with open(filename, 'w') as f:
                f.write(raw_xml)

            os.system('git config user.name "GitHub Actions Bot"')
            os.system('git config user.email "github-actions-bot@users.noreply.github.com"')
            os.system(f'git add {filename}')
            # Use || true in case the file hasn't changed since the last hour.
            os.system(f'git commit -m "UPDATE: Hourly vote record for resolution {resolution_id}" || true')
            os.system('git push')

            promoted_ts = int(resolution_tag.find('PROMOTED').text)
            # Resolutions last exactly 4 days (345600 seconds).
            TIME_TO_VOTE_END_SECONDS = 345600
            voting_end_timestamp = promoted_ts + TIME_TO_VOTE_END_SECONDS

            remaining_seconds = voting_end_timestamp - current_timestamp
            remaining_time = str(timedelta(seconds=remaining_seconds))
            end_dt = datetime.fromtimestamp(voting_end_timestamp, tz=TZ).strftime('%Y-%m-%d %H:%M:%S UTC')
            print(f"WA {council_id}: Successfully saved {filename}. {remaining_time} remaining. End: {end_dt}")


        except Exception as e:
            print(f"Error during execution/save for WA {council_id}: {e}")

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
    os.system(f'git add votes.csv resolutions.csv')
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