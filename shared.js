const CSV_SPLIT_REGEX = /,(?=(?:(?:[^"]*"){2})*[^"]*$)/;

function timestampToDate(timestamp) {
    if (!timestamp) return 'N/A';
    const date = new Date(parseInt(timestamp) * 1000);
    if (isNaN(date)) return 'N/A';
    const year = date.getUTCFullYear();
    const month = String(date.getUTCMonth() + 1).padStart(2, '0');
    const day = String(date.getUTCDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
}

function getChamber(council_id) {
    return council_id === '1' || council_id === '3' ? 'GA' : (council_id === '2' ? 'SC' : '');
}

function generateAuthorLinksHTML(authorsString) {
    if (!authorsString) {
        return '';
    }

    const authors = authorsString.split(',').map(author => author.trim());

    const linkedAuthors = authors.map(authorName => {
        const encodedName = encodeURIComponent(authorName);
        return `<a href="#nation=${encodedName}">${authorName}</a>`;
    });

    return linkedAuthors.join(', ');
}

async function loadAllData() {
    try {
        let response = await fetch('resolutions.csv');
        let text = await response.text();
        const resData = Papa.parse(text, { header: true, skipEmptyLines: true }).data;

        const resolutionsArray = resData.map(res => ({
            ...res,
            date_part: timestampToDate(res.promoted)
        }));

        const resolutionsMap = resolutionsArray.reduce((map, res) => {
            map[res.id] = res;
            return map;
        }, {});

        const voteTable = await loadWVDB();

        return {
            resolutionsArray: resolutionsArray,
            resolutionsMap: resolutionsMap,
            voteTable: voteTable
        };

    } catch (error) {
        console.error("Error loading application data:", error);
        throw new Error("Failed to load core data.");
    }
}

async function loadWVDB() {
    const response = await fetch('votes.wvdb');
    if (!response.ok) {
        throw new Error(`Unable to fetch WVDB data (${response.status}).`);
    }
    return WVDB.decode(new Uint8Array(await response.arrayBuffer()));
}
