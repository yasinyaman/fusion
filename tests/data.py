"""Sample datasets shared by application, inbound and e2e tests."""

USERS = [
    {"id": 1, "name": "Alice", "segment": "premium"},
    {"id": 2, "name": "Bob", "segment": "basic"},
    {"id": 3, "name": "Charlie", "segment": "premium"},
    {"id": 4, "name": "Diana", "segment": "standard"},
    {"id": 5, "name": "Eve", "segment": "basic"},
]
ORDERS = [
    {"id": 1, "user_id": 1, "amount": 100.0, "product": "A"},
    {"id": 2, "user_id": 2, "amount": 50.0, "product": "B"},
    {"id": 3, "user_id": 1, "amount": 200.0, "product": "A"},
    {"id": 4, "user_id": 3, "amount": 150.0, "product": "C"},
    {"id": 5, "user_id": 4, "amount": 75.0, "product": "B"},
    {"id": 6, "user_id": 5, "amount": 30.0, "product": "A"},
]
TEST_DB = {"users": USERS, "orders": ORDERS}

# A richer "Warp backend" for end-to-end scenarios.
MOCK_USERS = [
    {"id": 1, "name": "Alice", "email": "alice@test.com", "segment": "premium"},
    {"id": 2, "name": "Bob", "email": "bob@test.com", "segment": "basic"},
    {"id": 3, "name": "Charlie", "email": "charlie@test.com", "segment": "premium"},
    {"id": 4, "name": "Diana", "email": "diana@test.com", "segment": "standard"},
    {"id": 5, "name": "Eve", "email": "eve@test.com", "segment": "basic"},
]
MOCK_ORDERS = [
    {"id": 1, "user_id": 1, "amount": 250.0, "product": "Laptop", "status": "completed"},
    {"id": 2, "user_id": 2, "amount": 50.0, "product": "Mouse", "status": "completed"},
    {"id": 3, "user_id": 1, "amount": 120.0, "product": "Keyboard", "status": "pending"},
    {"id": 4, "user_id": 3, "amount": 800.0, "product": "Monitor", "status": "completed"},
    {"id": 5, "user_id": 4, "amount": 35.0, "product": "Cable", "status": "cancelled"},
    {"id": 6, "user_id": 5, "amount": 15.0, "product": "Mouse", "status": "completed"},
    {"id": 7, "user_id": 1, "amount": 300.0, "product": "SSD", "status": "completed"},
    {"id": 8, "user_id": 3, "amount": 60.0, "product": "Webcam", "status": "pending"},
]
MOCK_PRODUCTS = [
    {"id": 1, "name": "Laptop", "category": "electronics", "price": 250.0},
    {"id": 2, "name": "Mouse", "category": "accessories", "price": 50.0},
    {"id": 3, "name": "Keyboard", "category": "accessories", "price": 120.0},
    {"id": 4, "name": "Monitor", "category": "electronics", "price": 800.0},
    {"id": 5, "name": "Cable", "category": "accessories", "price": 35.0},
    {"id": 6, "name": "SSD", "category": "storage", "price": 300.0},
    {"id": 7, "name": "Webcam", "category": "accessories", "price": 60.0},
]
MOCK_DB = {"users": MOCK_USERS, "orders": MOCK_ORDERS, "products": MOCK_PRODUCTS}
