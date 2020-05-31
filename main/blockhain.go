package main

import (
	"bytes"
	"crypto/sha256"
)

//BlockChain type
type BlockChain struct {
	blocks []*Block //Una lista con las direcciones de memoria de cada bloque.
}

//Block type
type Block struct {
	Hash     []byte
	Data     []byte
	PrevHash []byte
}

//DeriveHash hash block, sha256 of data and prevHash
func (b *Block) DeriveHash() {
	info := bytes.Join([][]byte{b.Data, b.PrevHash}, []byte{})
	hash := sha256.Sum256(info)
	b.Hash = hash[:]
}

//CreateBlock with data and the prevHash
func CreateBlock(data string, prevHash []byte) *Block {
	block := &Block{[]byte{}, []byte(data), prevHash}
	block.DeriveHash()
	return block
}

//AddBlock metodo para tipo BlockChain.
func (chain *BlockChain) AddBlock(data string) {
	prevBlock := chain.blocks[len(chain.blocks)-1]
	new := CreateBlock(data, prevBlock.Hash)
	chain.blocks = append(chain.blocks, new)
}

//Genesis retorna el primer bloque.
func Genesis() *Block {
	return CreateBlock("Genesis", []byte{})
}

//InitBlockChain retorna el puntero a la blockchain.
func InitBlockChain() *BlockChain {
	return &BlockChain{[]*Block{Genesis()}}
}
