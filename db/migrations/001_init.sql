-- Migration: 001_init.sql
-- Description: Creates the loans table and seeds it with initial records if empty.

CREATE TABLE IF NOT EXISTS loans (
    id SERIAL PRIMARY KEY,
    borrower_name TEXT NOT NULL,
    loan_amount NUMERIC(14,2) NOT NULL,
    property_city TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO loans (borrower_name, loan_amount, property_city, status)
SELECT borrower_name, loan_amount, property_city, status
FROM (
    VALUES
        ('Aarav Sharma', 4500000.00, 'Mumbai', 'APPROVED'),
        ('Priya Patel', 2850000.50, 'Ahmedabad', 'PENDING'),
        ('Rohan Verma', 6200000.00, 'Bengaluru', 'APPROVED'),
        ('Ananya Iyer', 1750000.75, 'Chennai', 'REJECTED'),
        ('Vikram Malhotra', 5100000.00, 'Delhi', 'PENDING')
) AS seed(borrower_name, loan_amount, property_city, status)
WHERE NOT EXISTS (
    SELECT 1 FROM loans
);
