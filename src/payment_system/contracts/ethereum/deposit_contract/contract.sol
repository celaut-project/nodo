pragma solidity ^0.8.11;

contract Deposit {
    uint256 public parity_factor;
    address public owner;
    mapping(bytes32 => uint256) public token_list;

    event NewSession(
        bytes32 indexed token, 
        uint256 amount
    );


    constructor(uint256 pf) public {
        owner = msg.sender;
        parity_factor = pf;
    }

    function get_parity_factor() public view returns (uint256) {
        return parity_factor;
    }

    function add_gas(bytes32 token) public payable {
        token_list[token] += msg.value;

        emit NewSession(token, msg.value);
    }

    function get_gas(bytes32 token) public view returns (uint256) {
        return token_list[token];
    }

    function get_owner() public view returns (address) {
        return owner;
    }

    function transfer_property(address new_owner) public {
        require(msg.sender == owner);
        require(new_owner != owner);
        require(new_owner != address(0));

        owner = new_owner;
    }

    function refund_balance() public payable {
        require(msg.sender == owner);

        payable(owner).transfer(
            address(this).balance
        );
    }
}
