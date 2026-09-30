-- Make the test server's admin look like Azure Flexible Server's: not a superuser,
-- only CREATEDB + CREATEROLE. chat-dbinit must work with exactly that.
CREATE ROLE chatadmin LOGIN CREATEDB CREATEROLE PASSWORD 'admin-test-password';
