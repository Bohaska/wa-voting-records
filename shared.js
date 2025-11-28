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
        const resData = Papa.parse(text, {header: true}).data;

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
        const votesData = Papa.parse(text, {header: true});

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