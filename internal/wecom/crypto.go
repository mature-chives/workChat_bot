package wecom

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/sha1" // 企业微信回调协议固定使用 SHA-1。
	"crypto/subtle"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"sort"
)

const weComPaddingBlockSize = 32

type Crypto struct {
	token     string
	aesKey    []byte
	receiveID string
}

func NewCrypto(token, encodingAESKey, receiveID string) (*Crypto, error) {
	if token == "" || encodingAESKey == "" || receiveID == "" {
		return nil, errors.New("wecom crypto token, AES key and receive ID are required")
	}
	key, err := base64.StdEncoding.DecodeString(encodingAESKey + "=")
	if err != nil {
		return nil, fmt.Errorf("decode wecom AES key: %w", err)
	}
	if len(key) != 32 {
		return nil, fmt.Errorf("wecom AES key must decode to 32 bytes, got %d", len(key))
	}
	return &Crypto{token: token, aesKey: key, receiveID: receiveID}, nil
}

func (c *Crypto) VerifySignature(signature, timestamp, nonce, ciphertext string) bool {
	expected := c.Signature(timestamp, nonce, ciphertext)
	if len(signature) != len(expected) {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(signature), []byte(expected)) == 1
}

func (c *Crypto) Signature(timestamp, nonce, ciphertext string) string {
	parts := []string{c.token, timestamp, nonce, ciphertext}
	sort.Strings(parts)
	hash := sha1.New()
	for _, part := range parts {
		_, _ = hash.Write([]byte(part))
	}
	return hex.EncodeToString(hash.Sum(nil))
}

func (c *Crypto) Decrypt(ciphertext string) ([]byte, error) {
	encrypted, err := base64.StdEncoding.DecodeString(ciphertext)
	if err != nil {
		return nil, fmt.Errorf("decode wecom ciphertext: %w", err)
	}
	if len(encrypted) == 0 || len(encrypted)%aes.BlockSize != 0 {
		return nil, errors.New("invalid wecom ciphertext length")
	}

	block, err := aes.NewCipher(c.aesKey)
	if err != nil {
		return nil, fmt.Errorf("create AES cipher: %w", err)
	}
	plain := make([]byte, len(encrypted))
	cipher.NewCBCDecrypter(block, c.aesKey[:aes.BlockSize]).CryptBlocks(plain, encrypted)
	plain, err = unpad(plain, weComPaddingBlockSize)
	if err != nil {
		return nil, err
	}
	if len(plain) < 20 {
		return nil, errors.New("decrypted wecom payload is too short")
	}

	messageLength := int(binary.BigEndian.Uint32(plain[16:20]))
	messageEnd := 20 + messageLength
	if messageLength < 0 || messageEnd > len(plain) {
		return nil, errors.New("invalid wecom message length")
	}
	receivedID := string(plain[messageEnd:])
	if subtle.ConstantTimeCompare([]byte(receivedID), []byte(c.receiveID)) != 1 {
		return nil, errors.New("wecom receive ID mismatch")
	}
	message := make([]byte, messageLength)
	copy(message, plain[20:messageEnd])
	return message, nil
}

func unpad(input []byte, blockSize int) ([]byte, error) {
	if len(input) == 0 {
		return nil, errors.New("cannot unpad empty payload")
	}
	padding := int(input[len(input)-1])
	if padding < 1 || padding > blockSize || padding > len(input) {
		return nil, errors.New("invalid PKCS#7 padding")
	}
	for _, value := range input[len(input)-padding:] {
		if int(value) != padding {
			return nil, errors.New("invalid PKCS#7 padding bytes")
		}
	}
	return input[:len(input)-padding], nil
}
