package wecom

import (
	"crypto/aes"
	"crypto/cipher"
	"encoding/base64"
	"encoding/binary"
	"strings"
	"testing"
)

func TestDecryptAndVerifySignature(t *testing.T) {
	key := []byte("0123456789abcdef0123456789abcdef")
	encodingKey := strings.TrimSuffix(base64.StdEncoding.EncodeToString(key), "=")
	cryptoService, err := NewCrypto("callback-token", encodingKey, "corp-id")
	if err != nil {
		t.Fatalf("NewCrypto() error = %v", err)
	}
	ciphertext := encryptForTest(t, key, []byte("<xml>hello</xml>"), "corp-id")
	signature := cryptoService.Signature("100", "nonce", ciphertext)

	if !cryptoService.VerifySignature(signature, "100", "nonce", ciphertext) {
		t.Fatal("VerifySignature() rejected a valid signature")
	}
	if cryptoService.VerifySignature("bad", "100", "nonce", ciphertext) {
		t.Fatal("VerifySignature() accepted an invalid signature")
	}
	plain, err := cryptoService.Decrypt(ciphertext)
	if err != nil {
		t.Fatalf("Decrypt() error = %v", err)
	}
	if got, want := string(plain), "<xml>hello</xml>"; got != want {
		t.Fatalf("Decrypt() = %q, want %q", got, want)
	}
}

func TestDecryptRejectsWrongReceiveID(t *testing.T) {
	key := []byte("0123456789abcdef0123456789abcdef")
	encodingKey := strings.TrimSuffix(base64.StdEncoding.EncodeToString(key), "=")
	cryptoService, err := NewCrypto("callback-token", encodingKey, "corp-id")
	if err != nil {
		t.Fatalf("NewCrypto() error = %v", err)
	}
	ciphertext := encryptForTest(t, key, []byte("hello"), "other-corp")

	if _, err := cryptoService.Decrypt(ciphertext); err == nil ||
		!strings.Contains(err.Error(), "receive ID mismatch") {
		t.Fatalf("Decrypt() error = %v, want receive ID mismatch", err)
	}
}

func TestDecryptRejectsInvalidPadding(t *testing.T) {
	key := []byte("0123456789abcdef0123456789abcdef")
	encodingKey := strings.TrimSuffix(base64.StdEncoding.EncodeToString(key), "=")
	cryptoService, err := NewCrypto("callback-token", encodingKey, "corp-id")
	if err != nil {
		t.Fatalf("NewCrypto() error = %v", err)
	}
	invalidPlain := make([]byte, aes.BlockSize*2)
	invalidPlain[len(invalidPlain)-1] = 0
	ciphertext := encryptRawForTest(t, key, invalidPlain)

	if _, err := cryptoService.Decrypt(ciphertext); err == nil ||
		!strings.Contains(err.Error(), "padding") {
		t.Fatalf("Decrypt() error = %v, want padding error", err)
	}
}

func encryptForTest(t *testing.T, key, message []byte, receiveID string) string {
	t.Helper()
	payload := make([]byte, 20+len(message)+len(receiveID))
	copy(payload[:16], []byte("0123456789abcdef"))
	binary.BigEndian.PutUint32(payload[16:20], uint32(len(message)))
	copy(payload[20:], message)
	copy(payload[20+len(message):], receiveID)
	padding := weComPaddingBlockSize - len(payload)%weComPaddingBlockSize
	for range padding {
		payload = append(payload, byte(padding))
	}
	return encryptRawForTest(t, key, payload)
}

func encryptRawForTest(t *testing.T, key, plain []byte) string {
	t.Helper()
	block, err := aes.NewCipher(key)
	if err != nil {
		t.Fatalf("aes.NewCipher() error = %v", err)
	}
	if len(plain)%aes.BlockSize != 0 {
		t.Fatalf("plaintext length %d is not a multiple of AES block size", len(plain))
	}
	encrypted := make([]byte, len(plain))
	cipher.NewCBCEncrypter(block, key[:aes.BlockSize]).CryptBlocks(encrypted, plain)
	return base64.StdEncoding.EncodeToString(encrypted)
}
