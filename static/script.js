const assetInput = document.getElementById('barcode'); // Asset input field for check-in/check-out
const searchInput = document.getElementById('searchInput'); // Search input field for asset history
const toggleBtn = document.getElementById('toggle-btn'); // Button to toggle between check-in and check-out
const historyTable = document.getElementById('history-table'); // Table to display asset history

let currentAction = 'checkin'; // Default action

// Function to update the asset history table
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


// Function to fetch all assets and update the table
function fetchAllAssets() {
    fetch('/api/assets')
        .then(response => response.json())
        .then(data => updateHistoryTable(data))
        .catch(error => console.error('Error fetching assets:', error));
}

// Function to update asset status (add/check-in/check-out)
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
    .then(() => fetchAllAssets()) // Fetch and update all assets after successful operation
    .catch(error => console.error('Error:', error));
}

// Function to fetch history for a specific asset
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





// Event listener for asset input (check-in/check-out)
assetInput.addEventListener('keydown', event => {
    if (event.key === 'Enter') {
        event.preventDefault();
        const assetId = assetInput.value.trim();
        if (assetId) {
            updateAssetStatus(assetId, currentAction);
            assetInput.value = '';
        } else {
            console.error('Asset ID cannot be empty');
        }
    }
});

// Event listener for search input (asset history)
searchBtn.addEventListener('click', () => {
    const assetId = searchInput.value.trim();
    if (assetId) {
        fetchAssetHistory(assetId); // Function to fetch the history of a specific asset
    } else {
        console.error('Asset ID cannot be empty');
    }
});

// Event listener for toggle button
toggleBtn.addEventListener('click', () => {
    currentAction = currentAction === 'checkin' ? 'checkout' : 'checkin';
    toggleBtn.textContent = currentAction === 'checkin' ? 'Check In' : 'Check Out';
});

// Initial fetch to populate table
fetchAllAssets();

