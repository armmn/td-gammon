from ..game import Game

class HumanAgent(object):
    def __init__(self, player):
        self.player = player
        self.name = 'Human'

    def get_action(self, moves, game=None):
        print moves
        maxmoves = 0
        for mv in moves:
            maxmoves = max(maxmoves, len(mv))

        if not moves:
            raw_input("No moves for you...(hit enter)")
            return None

        while True:
            while True:
                mv1 = raw_input('Please enter a move "<location start>,<location end>" ("%s" for on the board, "%s" for off the board): ' % (Game.ON, Game.OFF))
                mv1 = self.get_formatted_move(mv1)
                if not mv1:
                    print 'Bad format enter e.g. "3,4"'
                else:
                    break

            while True:
                mv2 = raw_input('Please enter a second move (enter to skip): ')
                if mv2 == '':
                    mv2 = None
                    break
                mv2 = self.get_formatted_move(mv2)
                if not mv2:
                    print 'Bad format enter e.g. "3,4"'
                else:
                    break

            if maxmoves > 2:
                while True:
                    mv3 = raw_input('Please enter a third move (enter to skip): ')
                    if mv3 == '':
                        mv3 = None
                        break
                    mv3 = self.get_formatted_move(mv3)
                    if not mv3:
                        print 'Bad format enter e.g. "3,4"'
                    else:
                        break

                while True:
                    mv4 = raw_input('Please enter a fourth move (enter to skip): ')
                    if mv4 == '':
                        mv4 = None
                        break
                    mv4 = self.get_formatted_move(mv4)
                    if not mv4:
                        print 'Bad format enter e.g. "3,4"'
                    else:
                        break

                if mv4:
                    move = (mv1, mv2, mv3, mv4)
                else:
                    move = (mv1, mv2, mv3)

            else:
                if mv2:
                    move = (mv1, mv2)
                else:
                    move = (mv1,)

            if move in moves:
                return move
            elif move[::-1] in moves:
                move = move[::-1]
                return move
            else:
                print "You can't play that move"

        return None

    def get_formatted_move(self, move):
        try:
            start, end = move.split(",")
            if start == Game.ON:
                return (start, int(end))
            if end == Game.OFF:
                return (int(start), end)
            return (int(start), int(end))
        except:
            return False
