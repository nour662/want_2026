#!/bin/bash

echo "🚀 Setting up WANT FullStack Database..."

# Check if PostgreSQL is installed
if ! command -v psql &> /dev/null; then
    echo "❌ PostgreSQL is not installed. Installing via Homebrew..."
    brew install postgresql@15
    brew services start postgresql@15
else
    echo "✅ PostgreSQL is already installed"
fi

# Wait for PostgreSQL to be ready
sleep 2

# Create database
echo "📦 Creating database 'want_db'..."
psql postgres -c "CREATE DATABASE want_db;" 2>/dev/null || echo "Database already exists"

# Create user if needed
echo "👤 Setting up database user..."
psql postgres -c "CREATE USER postgres WITH PASSWORD 'postgres';" 2>/dev/null || echo "User already exists"
psql postgres -c "ALTER USER postgres WITH SUPERUSER;" 2>/dev/null

# Grant privileges
psql postgres -c "GRANT ALL PRIVILEGES ON DATABASE want_db TO postgres;" 2>/dev/null

echo "✅ Database setup complete!"
echo ""
echo "📝 Database connection details:"
echo "   Host: localhost"
echo "   Port: 5432"
echo "   Database: want_db"
echo "   User: postgres"
echo "   Password: postgres"
echo ""
echo "🔧 Connection string:"
echo "   postgresql://postgres:postgres@localhost:5432/want_db"
