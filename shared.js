// Regex to split only on commas that are NOT inside double quotes.
const CSV_SPLIT_REGEX = /,(?=(?:(?:[^"]*"){2})*[^"]*$)/;

/**
 * Converts a Unix timestamp (seconds) to a YYYY-MM-DD UTC date string.
 * @param {string} timestamp - The Unix timestamp string.
 * @returns {string} Formatted date string or 'N/A'.
 */
function timestampToDate(timestamp) {
    if (!timestamp) return 'N/A';
    const date = new Date(parseInt(timestamp) * 1000); // timestamp is in seconds
    if (isNaN(date)) return 'N/A';
    const year = date.getUTCFullYear();
    const month = String(date.getUTCMonth() + 1).padStart(2, '0');
    const day = String(date.getUTCDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
}

/**
 * Parses a CSV string into an array of objects, handling commas within double-quoted fields.
 * @param {string} text - The CSV content.
 * @returns {{headers: string[], data: Array<Object>}} Parsed data.
 */
function parseCSV(text) {
    const lines = text.trim().split('\n').filter(line => line.trim() !== '');
    if (lines.length < 2) return { headers: [], data: [] };

    const headers = lines[0].split(',').map(h => h.trim().replace(/"/g, ''));
    const data = [];

    for (let i = 1; i < lines.length; i++) {
        const line = lines[i].trim();
        const values = line.split(CSV_SPLIT_REGEX);

        if (values.length === headers.length) {
            const row = {};
            headers.forEach((header, index) => {
                let val = values[index].trim();
                // Strip outer quotes if present
                if (val.startsWith('"') && val.endsWith('"')) {
                    val = val.substring(1, val.length - 1);
                }
                row[header] = val;
            });
            data.push(row);
        } else {
             console.warn(`Skipping line ${i+1} in CSV: expected ${headers.length} columns, got ${values.length}. Line: ${line}`);
        }
    }
    return { headers, data };
}

/**
 * Converts council ID (1 or 2) to chamber name (GA or SC).
 * @param {string} council_id
 * @returns {string} Chamber name.
 */
function getChamber(council_id) {
    return council_id === '1' ? 'GA' : (council_id === '2' ? 'SC' : '');
}

/**
 * Fetches and processes all application data.
 * @returns {Promise<{ resolutionsArray: Array<Object>, resolutionsMap: Object, allVotes: Array<Object>, votesHeader: Array<string> }>}
 */
async function loadAllData() {
    try {
        // 1. Fetch and process resolutions
        let response = await fetch('resolutions.csv');
        let text = await response.text();
        const resData = parseCSV(text).data;

        const resolutionsArray = resData.map(res => ({
            ...res,
            date_part: timestampToDate(res.promoted) // Pre-calculate date
        }));

        const resolutionsMap = resolutionsArray.reduce((map, res) => {
            map[res.id] = res;
            return map;
        }, {});

        // 2. Fetch and process votes
        response = await fetch('votes.csv');
        text = await response.text();
        const votesData = parseCSV(text);

        return {
            resolutionsArray: resolutionsArray,
            resolutionsMap: resolutionsMap,
            allVotes: votesData.data,
            votesHeader: votesData.headers
        };

    } catch (error) {
        console.error("Error loading CSV data:", error);
        throw new Error("Failed to load core data.");
    }
}