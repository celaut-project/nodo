package node

type Image struct {
	MAIN_HASH string `json:"@main-hash256@"`

	API []string `json:"API"`

	BUILD string `json:"BUILD"`

	PKG []pkg `json:"PKG"`
}
