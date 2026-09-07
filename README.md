This project stores World Assembly voting data that is provided by NationStates. 

# Data pipeline

The hourly GitHub Actions workflow runs `scripts/monitor.py`. After rebuilding
`votes.csv`, the monitor verifies and atomically generates the compact
`votes.wvdb` artifact, then commits both datasets. GitHub Pages handles
transport compression for the static WVDB file. The website loads it in the
browser through `WVDB.js` and keeps the CSV available as a download.

To regenerate the binary files locally:

```text
python WVDB.py encode votes.csv votes.wvdb
```

# Copyright

The information provided in this repository is for general informational purposes only and does not constitute legal advice.

This website may display the following user-generated content provided by the NationStates API:
- Nation names
- Resolution names
- Nation votes

In most jurisdictions relevant to this website (the US, mainly), these are usually considered facts (nation votes) or not original enough (nation names, resolution names) to be protected by copyright.
