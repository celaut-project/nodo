# service.capnp
@0xc2e5ce59678781d6;
struct Map @0x84b07da5fac254b3 (Key, Value) {  # 0 bytes, 1 ptrs
  entries @0 :List(Entry);  # ptr[0]
  struct Entry @0xa363bb201ce59432 {  # 0 bytes, 2 ptrs
    key @0 :Key;  # ptr[0]
    value @1 :Value;  # ptr[1]
  }
}
struct Filesystem @0xe9cfc52d52153987 {  # 0 bytes, 1 ptrs
  branch @0 :List(ItemBranch);  # ptr[0]
  struct ItemBranch @0x854792c95d446ac8 {  # 8 bytes, 3 ptrs
    name @0 :Text;  # ptr[0]
    item :group {
      union {  # tag bits [0, 16)
        file @1 :List(Data);  # ptr[1], union tag = 0
        link :group {  # union tag = 1
          src @2 :Text;  # ptr[1]
          dst @3 :Text;  # ptr[2]
        }
        filesystem @4 :Filesystem;  # ptr[1], union tag = 2
      }
    }
  }
}
struct Container @0xd393b03e8ba7f0c8 {  # 0 bytes, 6 ptrs
  architecture @0 :Data;  # ptr[0]
  filesystem @1 :Filesystem;  # ptr[1]
  enviromentVariables @2 :Map(Text, Data);  # ptr[2]
  entrypoint @3 :List(Text);  # ptr[3]
  configuration @4 :Data;  # ptr[4]
  expectedGateway @5 :Data;  # ptr[5]
}
struct Service @0x8a3d618915535c50 {  # 0 bytes, 3 ptrs
  api @0 :Data;  # ptr[0]
  container @1 :Container;  # ptr[1]
  ledger @2 :Data;  # ptr[2]
}
struct ServiceWithMeta @0xbaba2cfc033c573d {  # 0 bytes, 2 ptrs
  metadata @0 :Data;  # ptr[0]
  service @1 :Service;  # ptr[1]
}