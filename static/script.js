document.addEventListener('DOMContentLoaded', () => {
    console.log('JavaScript file loaded'); // Initial log statement

    const toggleBtn = document.getElementById('toggle-btn');
    const barcodeInput = document.getElementById('barcode');
    const notification = document.getElementById('notification');
    const historyTableBody = document.querySelector('#history-table tbody');
    const searchBtn = document.getElementById('searchBtn');
    const searchInput = document.getElementById('searchInput');

    const showNotification = (message) => {
        notification.textContent = message;
        notification.style.display = 'block';
        setTimeout(() => {
            notification.style.display = 'none';
        }, 5000);
    };

    const handleCheckInOut = (changeButtonText) => {
        const assetId = barcodeInput.value;
        const action = toggleBtn.textContent.includes('Check In') ? 'checkin' : 'checkout';

        fetch(`/api/assets/${assetId}`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ action })
        })
        .then(response => response.json())
        .then(data => {
            console.log('API Response:', data); // Debugging line
            if (data.error) {
                showNotification(`Error: ${data.error}`);
            } else {
                showNotification(`Asset ${action} successfully: ${assetId}`);
                if (changeButtonText) {
                    toggleBtn.textContent = action === 'checkin' ? 'Check Out' : 'Check In';
                }
                fetchAssets();  // Refresh the asset list
            }
        })
        .catch(error => {
            showNotification(`Error: ${error}`);
        });
    };

    const fetchAssets = () => {
        fetch('/api/assets')
        .then(response => response.json())
        .then(data => {
            console.log('Fetched Assets:', data); // Debugging line
            historyTableBody.innerHTML = '';
            data.forEach(asset => {
                const row = document.createElement('tr');
                row.innerHTML = `
                    <td>${asset.asset_id}</td>
                    <td>${asset.check_in ? new Date(asset.check_in).toLocaleString() : ''}</td>
                    <td>${asset.check_out ? new Date(asset.check_out).toLocaleString() : ''}</td>
                `;
                historyTableBody.appendChild(row);
            });
        })
        .catch(error => {
            showNotification(`Error fetching assets: ${error}`);
        });
    };

    const searchAssetHistory = () => {
        const assetId = searchInput.value;
        if (!assetId) {
            showNotification('Please enter an Asset ID to search.');
            return;
        }
    
        window.location.href = `/asset_history?asset_id=${assetId}`;
    };
    
    searchBtn.addEventListener('click', searchAssetHistory);

    toggleBtn.addEventListener('click', () => handleCheckInOut(true));

    barcodeInput.addEventListener('keypress', (event) => {
        if (event.key === 'Enter') {
            event.preventDefault();  // Prevent form submission
            handleCheckInOut(false);
        }
    });

    searchBtn.addEventListener('click', searchAssetHistory);

    // Fetch assets on page load
    fetchAssets();
});