STORAGE MODE IN_MEMORY_ANALYTICAL;
CREATE INDEX ON :Bank(id);
CREATE INDEX ON :Merchant(id);
CREATE INDEX ON :SSN(ssn);
CREATE INDEX ON :Email(email);
CREATE INDEX ON :Phone(phoneNumber);
CREATE INDEX ON :Client(id);
CREATE INDEX ON :Transaction(id);
CREATE INDEX ON :Transaction(globalStep);
