// 메뉴 데이터 초기화
const menuDataElement = document.getElementById('menu-data');
const menuItems = JSON.parse(menuDataElement.dataset.menu);
const tableId = document.getElementById('table-data').dataset.tableId;

console.log('Loaded menuItems structure:', menuItems);

let orderItems = {};

function updateQuantity(itemId, change) {
    console.log('updateQuantity called with:', { itemId, change });
    
    const input = document.getElementById(`quantity-${itemId}`);
    if (!input) {
        console.error('Input element not found for itemId:', itemId);
        return;
    }
    
    const currentValue = parseInt(input.value) || 0;
    const newValue = Math.max(0, currentValue + change);
    
    console.log('Quantity update:', { currentValue, change, newValue });
    
    input.value = newValue;
    
    if (newValue > 0) {
        orderItems[itemId] = newValue;
    } else {
        delete orderItems[itemId];
    }
    
    console.log('Updated orderItems:', orderItems);
    updateOrderSummary();
}

function findMenuItem(itemId) {
    console.log('Searching for itemId:', itemId);
    
    // menuItems는 이미 플랫한 구조 (itemId가 키)
    const item = menuItems[itemId];
    if (item) {
        console.log('Found menu item:', { itemId, item });
        return item;
    }
    
    console.warn('Menu item not found:', itemId);
    return null;
}

function updateOrderSummary() {
    const summary = document.getElementById('order-summary');
    const totalAmount = document.getElementById('total-amount');
    const submitButton = document.getElementById('submit-order-btn');
    let total = 0;
    
    summary.innerHTML = '';
    
    for (const [itemId, quantity] of Object.entries(orderItems)) {
        const item = findMenuItem(itemId);
        if (!item) {
            console.error(`Menu item not found for ID: ${itemId}`);
            continue;
        }
        
        const itemTotal = item.price * quantity;
        total += itemTotal;
        console.log('Item total:', { itemId, quantity, price: item.price, itemTotal, runningTotal: total });
        
        const itemElement = document.createElement('div');
        itemElement.className = 'order-summary-item';
        itemElement.innerHTML = `
            <div class="d-flex justify-content-between align-items-center">
                <div>
                    <h6 class="mb-0">${item.name_kr}</h6>
                    <small class="text-muted">${item.name_en}</small>
                </div>
                <div class="text-end">
                    <div>${quantity}개</div>
                    <div class="text-primary">${itemTotal.toLocaleString()}원</div>
                </div>
            </div>
        `;
        summary.appendChild(itemElement);
    }
    
    console.log('Final total:', total);
    totalAmount.textContent = `${total.toLocaleString()}원`;
    submitButton.disabled = total === 0;
    console.log('Submit button disabled:', submitButton.disabled);
}

async function submitOrder() {
    const submitButton = document.getElementById('submit-order-btn');
    submitButton.disabled = true;
    submitButton.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>주문 처리 중...';
    
    try {
        const response = await fetch('/submit_order', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded'
            },
            body: new URLSearchParams({
                'table_id': tableId,
                'menu': JSON.stringify(orderItems)
            })
        });
        
        if (response.ok) {
            const result = await response.text();
            document.open();
            document.write(result);
            document.close();
        } else {
            throw new Error('주문 처리 중 오류가 발생했습니다.');
        }
    } catch (error) {
        alert(error.message);
        submitButton.disabled = false;
        submitButton.innerHTML = '<i class="bi bi-check-circle me-2"></i>주문하기';
    }
} 