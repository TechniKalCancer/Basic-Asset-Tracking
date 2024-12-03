document.addEventListener('DOMContentLoaded', () => {
    const checkinBtn = document.getElementById('checkin-btn');
    const checkoutBtn = document.getElementById('checkout-btn');
    const barcodeCheckinInput = document.getElementById('barcode-checkin');
    const barcodeCheckoutInput = document.getElementById('barcode-checkout');
    const notification = document.getElementById('notification');
    const checkinTableBody = document.querySelector('#checkin-table tbody');
    const checkoutTableBody = document.querySelector('#checkout-table tbody');
    const searchBtn = document.getElementById('searchBtn');
    const searchInput = document.getElementById('searchInput');

    const showNotification = (message) => {
        notification.textContent = message;
        notification.style.display = 'block';
        setTimeout(() => {
            notification.style.display = 'none';
        }, 5000);
    };

    const handleAction = (action, assetId) => {
        fetch(`/api/assets/${assetId}`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ action })
        })
        .then(response => response.json())
        .then(data => {
            if (data.error) {
                showNotification(`Error: ${data.error}`);
            } else {
                showNotification(`Asset ${action} successfully: ${assetId}`);
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
            checkinTableBody.innerHTML = '';
            checkoutTableBody.innerHTML = '';

            // Sort assets by most recent check-in
            const checkinAssets = data.filter(asset => asset.check_in).sort((a, b) => new Date(b.check_in) - new Date(a.check_in));
            checkinAssets.forEach(asset => {
                const row = document.createElement('tr');
                row.innerHTML = `
                    <td>${asset.asset_id}</td>
                    <td>${new Date(asset.check_in).toLocaleString()}</td>
                `;
                checkinTableBody.appendChild(row);
            });

            // Sort assets by most recent check-out
            const checkoutAssets = data.filter(asset => asset.check_out).sort((a, b) => new Date(b.check_out) - new Date(a.check_out));
            checkoutAssets.forEach(asset => {
                const row = document.createElement('tr');
                row.innerHTML = `
                    <td>${asset.asset_id}</td>
                    <td>${new Date(asset.check_out).toLocaleString()}</td>
                `;
                checkoutTableBody.appendChild(row);
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

    if (checkinBtn) {
        checkinBtn.addEventListener('click', () => handleAction('checkin', barcodeCheckinInput.value));
    }

    if (checkoutBtn) {
        checkoutBtn.addEventListener('click', () => handleAction('checkout', barcodeCheckoutInput.value));
    }

    barcodeCheckinInput.addEventListener('keypress', (event) => {
        if (event.key === 'Enter') {
            event.preventDefault();  // Prevent form submission
            handleAction('checkin', barcodeCheckinInput.value);
            barcodeCheckinInput.value = '';  // Clear the input field
        }
    });

    barcodeCheckoutInput.addEventListener('keypress', (event) => {
        if (event.key === 'Enter') {
            event.preventDefault();  // Prevent form submission
            handleAction('checkout', barcodeCheckoutInput.value);
            barcodeCheckoutInput.value = '';  // Clear the input field
        }
    });

    // Fetch assets on page load
    fetchAssets();
});