const assetInput = document.getElementById('barcode');
const toggleBtn = document.getElementById('toggle-btn');
const historyTable = document.getElementById('history-table');

let currentAction = 'checkin'; // Default action

// Function to update asset history table
function updateHistoryTable(data) {
    historyTable.innerHTML = '';
    for (const item of data) {
        const row = document.createElement('tr');
        const assetCell = document.createElement('td');
        const checkinCell = document.createElement('td');
        const checkoutCell = document.createElement('td');

        assetCell.textContent = item.asset;
        checkinCell.textContent = item.check_in || '-';
        checkoutCell.textContent = item.check_out || '-';

        row.appendChild(assetCell);
        row.appendChild(checkinCell);
        row.appendChild(checkoutCell);

        historyTable.appendChild(row);
    }
}

// Event listener for barcode scan
assetInput.addEventListener('change', () => {
    const barcode = assetInput.value.trim();
    if (barcode) {
        // Make API call to update check-in/check-out status
        updateAssetStatus(barcode, currentAction);
        // Clear the input field
        assetInput.value = '';
    } else {
        console.error('Asset ID cannot be empty');
    }
});

// Event listener for "Enter" key press on the input field
assetInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
        // Toggle between Check In and Check Out
        currentAction = currentAction === 'checkin' ? 'checkout' : 'checkin';
        toggleBtn.textContent = currentAction === 'checkin' ? 'Check In' : 'Check Out';

        // Prevent the default behavior (form submission)
        event.preventDefault();

        // Clear the input field
        assetInput.value = '';

        // Make API call to update check-in/check-out status
        updateAssetStatus(assetInput.value.trim(), currentAction);
    }
});

// Event listener for toggle button
toggleBtn.addEventListener('click', () => {
    // Toggle between Check In and Check Out
    currentAction = currentAction === 'checkin' ? 'checkout' : 'checkin';
    toggleBtn.textContent = currentAction === 'checkin' ? 'Check In' : 'Check Out';
});

// Function to update asset status through API
function updateAssetStatus(assetId, action) {
    // Log assetId to verify it
    console.log('Asset ID:', assetId);

    // Make API call to update check-in/check-out status
    fetch(`/api/assets/${assetId}`, {  // Ensure assetId is included in the URL
        method: 'POST',
        body: JSON.stringify({ action }),
        headers: {
            'Content-Type': 'application/json',
        },
    })
    .then(response => {
        console.log('API response:', response);
        if (!response.ok) {
            throw new Error('Failed to update asset status');
        }
        return response.json();
    })
    .then(data => {
        console.log('Data received from server:', data);
        // Fetch and update asset history
        fetch('/api/assets')
            .then(response => response.json())
            .then(data => updateHistoryTable(data));
    })
    .catch(error => {
        console.error('Error updating asset status:', error);
    });
}

// Fetch and display initial asset history
fetch('/api/assets')
    .then(response => response.json())
    .then(data => updateHistoryTable(data));
