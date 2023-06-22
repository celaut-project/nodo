import sys

if __name__ == '__main__':
    if len(sys.argv) == 1:
        print("Command needed: "
              "\n- seeder"
              "\n- connect"
              "\n- serve"
              "\n- migrate"
              "\n- command:containers --stream"
              "\n- command:peers --stream"
              "\n- prune:peer <peer_id>"
              "\n- command:ledgers --stream"
              "\n- view:contract"
              )

    elif sys.argv[1] == "seeder":
        from contracts.eth_main.seeder import seed
        seed() if len(sys.argv) == 2 else seed(private_key=sys.argv[2])

    elif sys.argv[1] == 'connect':
        from src.utils.zeroconf import connect
        connect(sys.argv[2])

    elif sys.argv[1] == 'serve':
        from src.serve import serve
        serve()

    elif sys.argv[1] == 'migrate':
        import os
        from database.migrate import migrate
        from contracts.eth_main.seeder import seed
        os.system("rm database.sqlite")
        migrate()
        seed() if len(sys.argv) == 2 else seed(private_key=sys.argv[2])

    elif sys.argv[1] == 'command:containers':
        from commands.containers import containers
        containers(stream=len(sys.argv) == 3 and sys.argv[2] == '--stream')

    elif sys.argv[1] == 'command:peers':
        from commands.peers import peers
        peers(stream=len(sys.argv) == 3 and sys.argv[2] == '--stream')

    elif sys.argv[1] == 'prune:peer':
        from commands.peers import delete
        delete(sys.argv[2])

    elif sys.argv[1] == 'command:ledgers':
        from commands.ledgers import ledgers
        ledgers(stream=len(sys.argv) == 3 and sys.argv[2] == '--stream')

    elif sys.argv[1] == 'view:contract':
        from commands.ledgers import view
        view(sys.argv[2])

    else:
        print('Unknown command.')


""" > python3.10
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

        case other:
            print('Unknown command.')

"""
