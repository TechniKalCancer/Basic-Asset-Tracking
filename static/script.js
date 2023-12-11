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

        // Check if 'checkin' or 'checkout' events exist in the history
        const checkinEvent = item.history.find(event => event.action === 'checkin');
        const checkoutEvent = item.history.find(event => event.action === 'checkout');

        checkinCell.textContent = checkinEvent ? checkinEvent.timestamp : '-';
        checkoutCell.textContent = checkoutEvent ? checkoutEvent.timestamp : '-';

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
        // Prevent the default behavior (form submission)
        event.preventDefault();
        // Make API call to update check-in/check-out status
        updateAssetStatus(assetInput.value.trim(), currentAction);
        // Clear the input field
        assetInput.value = '';
    }
});

// Event listener for toggle button
toggleBtn.addEventListener('click', () => {
    currentAction = currentAction === 'checkin' ? 'checkout' : 'checkin';
    toggleBtn.textContent = currentAction === 'checkin' ? 'Check In' : 'Check Out';
});

// Function to update asset status through API
function updateAssetStatus(assetId, action) {
    // Log assetId to verify it
    console.log('Asset ID:', assetId);

    // Make API call to update check-in/check-out status
    fetch(`/api/assets/${assetId}`, {
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

        // Display notification
        showNotification(`Asset ${data.asset.asset} updated successfully`, 'green', 5000);

        // Fetch and update asset history
        fetch('/api/assets')
            .then(response => response.json())
            .then(data => updateHistoryTable(data));
    })
    .catch(error => {
        console.error('Error updating asset status:', error);
        // Display error notification
        showNotification('Error updating asset status', 'red', 5000);
    });
}

// Function to display notification
function showNotification(message, color, timeout) {
    const notification = document.createElement('div');
    notification.style.backgroundColor = color;
    notification.style.color = 'white';
    notification.style.padding = '10px';
    notification.style.borderRadius = '5px';
    notification.style.position = 'fixed';
    notification.style.top = '10px';
    notification.style.right = '10px';
    notification.textContent = message;
    document.body.appendChild(notification);

    // Automatically hide after a timeout
    setTimeout(() => {
        document.body.removeChild(notification);
    }, timeout);
}

// Fetch and display initial asset history
fetch('/api/assets')
    .then(response => response.json())
    .then(data => updateHistoryTable(data));
