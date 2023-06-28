import sys

if __name__ == '__main__':
    if len(sys.argv) == 1:
        print("Command needed: "
              "\n- seeder"
              "\n- connect"
              "\n- serve"
              "\n- migrate"
              "\n- containers --stream"
              "\n- peers --stream"
              "\n- prune peer <peer_id>"
              "\n- ledgers --stream"
              "\n- view contract"
              )
    else:
        match sys.argv[1]:

            case "seeder":
                from contracts.eth_main.seeder import seed
                seed() if len(sys.argv) == 2 else seed(private_key=sys.argv[2])

            case 'connect':
                from src.utils.zeroconf import connect
                connect(sys.argv[2])

            case 'serve':
                from src.serve import serve
                serve()

            case 'migrate':
                import os
                from database.migrate import migrate
                from contracts.eth_main.seeder import seed
                os.system("rm database.sqlite")
                migrate()
                seed() if len(sys.argv) == 2 else seed(private_key=sys.argv[2])

            case 'containers':
                from commands.containers import containers
                containers(stream=len(sys.argv) == 3 and sys.argv[2] == '--stream')

            case 'peers':
                from commands.peers import peers
                peers(stream=len(sys.argv) == 3 and sys.argv[2] == '--stream')

            case 'prune peer':
                from commands.peers import delete
                delete(sys.argv[2])

            case 'ledgers':
                from commands.ledgers import ledgers
                ledgers(stream=len(sys.argv) == 3 and sys.argv[2] == '--stream')

            case 'view contract':
                from commands.ledgers import view
                view(sys.argv[2])

            case other:
                print('Unknown command.')
