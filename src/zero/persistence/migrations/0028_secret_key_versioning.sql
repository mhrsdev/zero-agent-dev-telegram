-- Secret key versioning: stamp the encryption-key identity used for each
-- stored ciphertext so rotating ZERO_SECRET_KEY produces a precise,
-- actionable resolution error instead of an undecryptable mystery.

ALTER TABLE secret_references
    ADD COLUMN key_id TEXT;
