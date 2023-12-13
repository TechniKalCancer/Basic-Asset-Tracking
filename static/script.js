const assetInput = document.getElementById('barcode');
const toggleBtn = document.getElementById('toggle-btn');
const historyTable = document.getElementById('history-table');
const searchInput = document.getElementById('searchInput');
const searchBtn = document.getElementById('searchBtn');

let currentAction = 'checkin'; // Default action

// Function to update asset history table
function updateHistoryTable(data) {
    // Get the tbody element of the table
    const tbody = document.querySelector('#history-table tbody');
    
    // Clear existing content in tbody
    tbody.innerHTML = '';

    // Add new rows to tbody based on the fetched data
    data.forEach(item => {
        const row = document.createElement('tr');
        Object.values(item).forEach(value => {
            const cell = document.createElement('td');
            cell.textContent = value;
            row.appendChild(cell);
        });
        tbody.appendChild(row);
    });
}


fetch('/api/assets')
    .then(response => response.json())
    .then(data => {
        updateHistoryTable(data);  // Make sure this function is defined correctly
    })
    .catch(error => console.error('Error:', error));

// Function to make API call and update asset status
function updateAssetStatus(assetId, action) {
    fetch(`/api/assets/${assetId}`, {
        method: 'POST',
        body: JSON.stringify({ action }),
        headers: {'Content-Type': 'application/json'},
    })
    .then(response => {
        if (!response.ok) throw new Error('Failed to update asset status');
        return response.json();
    })
    .then(data => {
        fetchAssetHistory(); // Refresh the asset history table
    })
    .catch(error => console.error('Error:', error));
}

function fetchAssetHistory() {
    fetch('/api/assets')
        .then(response => response.json())
        .then(data => {
            updateHistoryTable(data);
        })
        .catch(error => console.error('Error fetching asset history:', error));
}

// Event listener for barcode input
assetInput.addEventListener('change', () => {
    const barcode = assetInput.value.trim();
    if (barcode) {
        updateAssetStatus(barcode, currentAction);
        assetInput.value = '';
    } else {
        console.error('Asset ID cannot be empty');
    }
});

// Event listener for barcode input "Enter" key press
assetInput.addEventListener('keydown', event => {
    if (event.key === 'Enter') {
        event.preventDefault();
        const barcode = assetInput.value.trim();
        if (barcode) {
            updateAssetStatus(barcode, currentAction);
            assetInput.value = '';
        } else {
            console.error('Asset ID cannot be empty');
        }
    }
});

function fetchAssetHistory(assetId) {
    fetch(`/asset_history?asset_id=${assetId}`)
        .then(response => response.json())
        .then(data => {
            if (data && data.history) {
                updateHistoryTable(data.history);
            } else {
                console.error('No history found or invalid response:', data);
            }
        })
        .catch(error => console.error('Error fetching asset history:', error));
}

// Event listener for toggle button
toggleBtn.addEventListener('click', () => {
    if (currentAction === 'checkin') {
        currentAction = 'checkout';
        toggleBtn.textContent = 'Check Out';
    } else {
        currentAction = 'checkin';
        toggleBtn.textContent = 'Check In';
    }
});

// Event listener for search button
searchBtn.addEventListener('click', () => {
    const assetId = searchInput.value.trim();
    if (assetId) {
        fetchAssetHistory(assetId);
        searchInput.value = '';
    } else {
        console.error('Asset ID cannot be empty for search');
    }
});

// Event listener for search input "Enter" key press
searchInput.addEventListener('keydown', event => {
    if (event.key === 'Enter') {
        event.preventDefault();
        const assetId = searchInput.value.trim();
        if (assetId) {
            fetchAssetHistory(assetId);
            searchInput.value = '';
        } else {
            console.error('Asset ID cannot be empty for search');
        }
    }
});