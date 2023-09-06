import json5 as json
import csv
import os
import datetime
import pytz
import timedelta
import numpy as np
import pulp as plp
from itertools import groupby
from random import shuffle, choice


class NFL_Optimizer:
    site = None
    config = None
    problem = None
    output_dir = None
    num_lineups = None
    num_uniques = None
    team_list = []
    players_by_team = {}
    lineups = {}
    player_dict = {}
    at_least = {}
    at_most = {}
    team_limits = {}
    matchup_limits = {}
    matchup_at_least = {}
    stack_rules = {}
    global_team_limit = None
    use_double_te = True
    projection_minimum = 0
    randomness_amount = 0
    team_rename_dict = {
        "LA": "LAR"
    }

    def __init__(self, site=None, num_lineups=0, num_uniques=1):
        self.site = site
        self.num_lineups = int(num_lineups)
        self.num_uniques = int(num_uniques)
        self.load_config()
        self.load_rules()

        self.problem = plp.LpProblem('NFL', plp.LpMaximize)

        projection_path = os.path.join(os.path.dirname(
            __file__), '../{}_data/{}'.format(site, self.config['projection_path']))
        self.load_projections(projection_path)

        player_path = os.path.join(os.path.dirname(
            __file__), '../{}_data/{}'.format(site, self.config['player_path']))
        self.load_player_ids(player_path)
        

    def flatten(self, list):
        return [item for sublist in list for item in sublist]

    # Load config from file
    def load_config(self):
        with open(os.path.join(os.path.dirname(__file__), '../config.json')) as json_file:
            self.config = json.load(json_file)

    # Load player IDs for exporting
    def load_player_ids(self, path):
        with open(path) as file:
            reader = csv.DictReader(file)
            for row in reader:
                name_key = 'Name' if self.site == 'dk' else 'Nickname'
                player_name = row[name_key].replace('-', '#').lower().strip()
                position = row['Roster Position'].split('/')[0]
                team = row['TeamAbbrev']
                if (player_name, position, team) in self.player_dict:
                    matchup =  row['Game Info'].split(' ')[0]
                    teams = matchup.split('@')
                    opponent = teams[0] if teams[0] != team else teams[1]
                    self.player_dict[(player_name, position, team)]['Opponent'] = opponent
                    self.player_dict[(player_name, position, team)]['Matchup'] = matchup
                    if self.site == 'dk':
                        self.player_dict[(player_name, position, team)]['ID'] = int(
                            row['ID'])
                    else:
                        self.player_dict[(player_name, position, team)]['ID'] = int(
                            row['Id'].split('-')[1])

    def load_rules(self):
        self.at_most = self.config["at_most"]
        self.at_least = self.config["at_least"]
        self.team_limits = self.config["team_limits"]
        self.global_team_limit = int(self.config["global_team_limit"])
        self.projection_minimum = int(self.config["projection_minimum"])
        self.randomness_amount = float(self.config["randomness"])
        self.use_double_te = bool(self.config["use_double_te"])
        self.stack_rules = self.config["stack_rules"]
        self.matchup_at_least = self.config["matchup_at_least"]
        self.matchup_limits = self.config["matchup_limits"]

    # Load projections from file
    def load_projections(self, path):
        # Read projections into a dictionary
        with open(path, encoding='utf-8-sig') as file:
            reader = csv.DictReader(file)
            for row in reader:
                player_name = row['Name'].replace('-', '#').lower().strip()
                position = row['Position']
                team = row['Team']
                if team in self.team_rename_dict:
                    team = self.team_rename_dict[team]
                
                stddev = row['StdDev'] if 'StdDev' in row else 0
                if stddev == '':
                    stddev = 0
                else:
                    stddev = float(stddev)
                ceiling = row['Ceiling'] if 'Ceiling' in row else row['Fpts'] + stddev
                if float(row['Fpts']) < self.projection_minimum and row['Position'] != 'DST':
                    continue
                
                self.player_dict[(player_name, position, team)] = {
                    'Fpts': float(row['Fpts']),
                    'Position': position,
                    'ID': 0,
                    'Salary': int(row['Salary']),
                    'Name': row['Name'],
                    'Matchup': '',
                    'Team': team,
                    'Ownership': float(row['Own%']) if float(row['Own%']) != 0 else 0.1,
                    'Ceiling': ceiling,
                    'StdDev': stddev,
                }
                
                if team not in self.team_list:
                    self.team_list.append(team)
                    
                if team not in self.players_by_team:
                    self.players_by_team[team] = {
                        'QB': [], 'RB': [], 'WR': [], 'TE': [], 'DST': []
                    }
                
                self.players_by_team[team][position].append(self.player_dict[(player_name, position, team)])

    def optimize(self):
        # Setup our linear programming equation - https://en.wikipedia.org/wiki/Linear_programming
        # We will use PuLP as our solver - https://coin-or.github.io/pulp/

        # We want to create a variable for each roster slot.
        # There will be an index for each player and the variable will be binary (0 or 1) representing whether the player is included or excluded from the roster.
        lp_variables = {self.player_dict[(player, pos_str, team)]['ID']: plp.LpVariable(
            str(self.player_dict[(player, pos_str, team)]['ID']), cat='Binary'
            ) for (player, pos_str, team) in self.player_dict}

        # set the objective - maximize fpts & set randomness amount from config
        if self.randomness_amount != 0:
            self.problem += plp.lpSum(np.random.normal(self.player_dict[(player, pos_str, team)]['Fpts'],
                                                        (self.player_dict[(player, pos_str, team)]['StdDev'] * self.randomness_amount / 100))
                                        * lp_variables[self.player_dict[(player, pos_str, team)]['ID']]
                             for (player, pos_str, team) in self.player_dict), 'Objective'
        else:
            self.problem += plp.lpSum(self.player_dict[(player, pos_str, team)]['Fpts'] * lp_variables[self.player_dict[(player, pos_str, team)]['ID']]
                             for (player, pos_str, team) in self.player_dict), 'Objective'
        
        # Set the salary constraints
        max_salary = 50000 if self.site == 'dk' else 60000
        min_salary = 45000 if self.site == 'dk' else 55000
        self.problem += plp.lpSum(self.player_dict[(player, pos_str, team)]['Salary'] *
                                  lp_variables[self.player_dict[(player, pos_str, team)]['ID']] for (player, pos_str, team) in self.player_dict) <= max_salary, 'Max Salary'
        self.problem += plp.lpSum(self.player_dict[(player, pos_str, team)]['Salary'] *
                                  lp_variables[self.player_dict[(player, pos_str, team)]['ID']] for (player, pos_str, team) in self.player_dict) >= min_salary, 'Min Salary'

        # Address limit rules if any
        for limit, groups in self.at_least.items():
            for group in groups:
                tuple_name_list = []
                for key, value in self.player_dict.items():
                    if value['Name'] in group:
                        tuple_name_list.append(key)
                        
                self.problem += plp.lpSum(lp_variables[self.player_dict[(player, pos_str, team)]['ID']]
                                          for (player, pos_str, team) in tuple_name_list) >= int(limit), f'At least {limit} players {tuple_name_list}'

        for limit, groups in self.at_most.items():
            for group in groups:
                tuple_name_list = []
                for key, value in self.player_dict.items():
                    if value['Name'] in group:
                        tuple_name_list.append(key)
                        
                self.problem += plp.lpSum(lp_variables[self.player_dict[(player, pos_str, team)]['ID']]
                                          for (player, pos_str, team) in tuple_name_list) <= int(limit), f'At most {limit} players {tuple_name_list}'

        # Address team limits
        for team, limit in self.team_limits.items():
            self.problem += plp.lpSum(lp_variables[self.player_dict[(player, pos_str, team)]['ID']]
                                      for (player, pos_str, team) in self.player_dict if self.player_dict[(player, pos_str, team)]['Team'] == team) <= int(limit), f'Team limit {team} {limit}'

        if self.global_team_limit is not None:
            for team in self.team_list:
                self.problem += plp.lpSum(lp_variables[self.player_dict[(player, pos_str, team)]['ID']]
                                          for (player, pos_str, team) in self.player_dict if self.player_dict[(player, pos_str, team)]['Team'] == team) <= int(self.global_team_limit), f'Global team limit {team} {self.global_team_limit}'
                
        # Address matchup limits
        if self.matchup_limits is not None:
            for matchup, limit in self.matchup_limits.items():
                players_in_game = []
                for key, value in self.player_dict.items():
                    if value['Matchup'] == matchup:
                        players_in_game.append(key)
                self.problem += plp.lpSum(lp_variables[self.player_dict[(player, pos_str, team)]['ID']] for (player, pos_str, team) in players_in_game) <= int(limit), f'Matchup limit {matchup} {limit}'
        
        if self.matchup_at_least is not None:
            for matchup, limit in self.matchup_limits.items():
                players_in_game = []
                for key, value in self.player_dict.items():
                    if value['Matchup'] == matchup:
                        players_in_game.append(key)
                self.problem += plp.lpSum(lp_variables[self.player_dict[(player, pos_str, team)]['ID']] for (player, pos_str, team) in players_in_game) >= int(limit), f'Matchup at least {matchup} {limit}'
                    
        # Address stack rules
        for rule_type in self.stack_rules:
            for rule in self.stack_rules[rule_type]:
                if rule_type == 'pair':
                    pos_key = rule['key']
                    stack_positions = rule['positions']
                    count = rule['count']
                    stack_type = rule['type']
                    excluded_teams = rule['exclude_teams']
                    
                    # Iterate each team, less excluded teams, and apply the rule for each key player pos
                    for team in self.players_by_team:
                        if team in excluded_teams:
                            continue
                        
                        pos_key_player = self.players_by_team[team][pos_key][0]
                        opp_team = pos_key_player['Opponent']
                        
                        stack_players = []
                        if stack_type == 'same-team':
                            for pos in stack_positions:
                                stack_players.append(self.players_by_team[team][pos])
                                
                        elif stack_type == 'opp-team':
                            for pos in stack_positions:
                                stack_players.append(self.players_by_team[opp_team][pos])
                                
                        elif stack_type == 'same-game':
                            for pos in stack_positions:
                                stack_players.append(self.players_by_team[team][pos])
                                stack_players.append(self.players_by_team[opp_team][pos])
                                
                        stack_players = self.flatten(stack_players)
                        # player cannot exist as both pos_key_player and be present in the stack_players
                        stack_players = [
                            p for p in stack_players 
                            if not (p['Name'] == pos_key_player['Name'] and p['Position'] == pos_key_player['Position'] and p['Team'] == pos_key_player['Team'])
                        ]
                        pos_key_player_tuple = None
                        stack_players_tuples = []
                        for key, value in self.player_dict.items():
                            if value['Name'] == pos_key_player['Name'] and value['Position'] == pos_key_player['Position'] and value['Team'] == pos_key_player['Team']:
                                pos_key_player_tuple = key
                            elif (value['Name'], value['Position'], value['Team']) in [(player['Name'], player['Position'], player['Team']) for player in stack_players]:
                                stack_players_tuples.append(key)

                        # [sum of stackable players] + -n*[stack_player] >= 0
                        self.problem += plp.lpSum([lp_variables[self.player_dict[(player, pos_str, team)]['ID']] for (player, pos_str, team) in stack_players_tuples] 
                                                  + [-count*lp_variables[self.player_dict[pos_key_player_tuple]['ID']]]) >= 0, f'Stack rule {pos_key_player_tuple} {stack_players_tuples} {count}'
                        
                elif rule_type == 'limit':
                    limit_positions = rule['positions'] # ["RB"]
                    stack_type = rule['type']
                    count = rule['count']
                    excluded_teams = rule['exclude_teams']
                    if 'unless_positions' in rule or 'unless_type' in rule:
                        unless_positions = rule['unless_positions']
                        unless_type = rule['unless_type']
                    else:
                        unless_positions = None
                        unless_type = None
                    
                    opp_team = self.players_by_team[team]['QB'][0]['Opponent']
                    
                    # Iterate each team, less excluded teams, and apply the rule for each key player pos
                    for team in self.players_by_team:
                        if team in excluded_teams:
                            continue
                        
                        limit_players = []
                        if stack_type == 'same-team':
                            for pos in limit_positions:
                                limit_players.append(self.players_by_team[team][pos])
                                
                        elif stack_type == 'opp-team':
                            for pos in limit_positions:
                                limit_players.append(self.players_by_team[opp_team][pos])
                                
                        elif stack_type == 'same-game':
                            for pos in limit_positions:
                                limit_players.append(self.players_by_team[team][pos])
                                limit_players.append(self.players_by_team[opp_team][pos])
                                
                        limit_players = self.flatten(limit_players)
                        if unless_positions is None or unless_type is None:
                            # [sum of limit players] + <= n
                            limit_players_tuples = []
                            for key, value in self.player_dict.items():
                                if (value['Name'], value['Position'], value['Team']) in [(player['Name'], player['Position'], player['Team']) for player in limit_players]:
                                    limit_players_tuples.append(key)
                            
                            self.problem += plp.lpSum([lp_variables[self.player_dict[(player, pos_str, team)]['ID']] for (player, pos_str, team) in limit_players_tuples]) <= int(count), f'Limit rule {limit_players_tuples} {count}'
                        else:
                            unless_players = []
                            if unless_type == 'same-team':
                                for pos in unless_positions:
                                    unless_players.append(self.players_by_team[team][pos])
                            elif unless_type == 'opp-team':
                                for pos in unless_positions:
                                    unless_players.append(self.players_by_team[opp_team][pos])
                            elif unless_type == 'same-game':
                                for pos in unless_positions:
                                    unless_players.append(self.players_by_team[team][pos])
                                    unless_players.append(self.players_by_team[opp_team][pos])
                                    
                            unless_players = self.flatten(unless_players)
                            
                            # player cannot exist as both limit_players and unless_players
                            unless_players = [
                                p for p in unless_players
                                if not any(
                                    p['Name'] == key_player['Name'] and p['Position'] == key_player['Position'] and p['Team'] == key_player['Team'] 
                                    for key_player in limit_players
                                )
                                
                            ]
                            
                            limit_players_tuples = []
                            unless_players_tuples = []
                            for key, value in self.player_dict.items():
                                if (value['Name'], value['Position'], value['Team']) in [(player['Name'], player['Position'], player['Team']) for player in limit_players]:
                                    limit_players_tuples.append(key)
                                elif (value['Name'], value['Position'], value['Team']) in [(player['Name'], player['Position'], player['Team']) for player in unless_players]:
                                    unless_players_tuples.append(key)
                                    
                            # [sum of limit players] + -count(unless_players)*[unless_players] <= n
                            self.problem += plp.lpSum([lp_variables[self.player_dict[(player, pos_str, team)]['ID']] for (player, pos_str, team) in limit_players_tuples]
                                                    + [-1*[lp_variables[self.player_dict[(player, pos_str, team)]['ID']] for (player, pos_str, team) in unless_players_tuples]]) <= int(count), f'Limit rule {limit_players_tuples} unless {unless_players_tuples} {count}'
                        
                        
        # Need exactly 1 QB
        self.problem += plp.lpSum(lp_variables[self.player_dict[(player, pos_str, team)]['ID']]
                                    for (player, pos_str, team) in self.player_dict if 'QB' == self.player_dict[(player, pos_str, team)]['Position']) == 1, f'QB limit 1'

        # Need at least 2 RB, up to 3 if using FLEX
        self.problem += plp.lpSum(lp_variables[self.player_dict[(player, pos_str, team)]['ID']]
                                    for (player, pos_str, team) in self.player_dict if 'RB' == self.player_dict[(player, pos_str, team)]['Position']) >= 2, f'RB >= 2'
        self.problem += plp.lpSum(lp_variables[self.player_dict[(player, pos_str, team)]['ID']]
                                    for (player, pos_str, team) in self.player_dict if 'RB' == self.player_dict[(player, pos_str, team)]['Position']) <= 3, f'RB <= 3'

        # Need at least 3 WR, up to 4 if using FLEX
        self.problem += plp.lpSum(lp_variables[self.player_dict[(player, pos_str, team)]['ID']]
                                    for (player, pos_str, team) in self.player_dict if 'WR' == self.player_dict[(player, pos_str, team)]['Position']) >= 3, f'WR >= 3'
        self.problem += plp.lpSum(lp_variables[self.player_dict[(player, pos_str, team)]['ID']]
                                    for (player, pos_str, team) in self.player_dict if 'WR' == self.player_dict[(player, pos_str, team)]['Position']) <= 4, f'WR <= 4'

        # Need at least 1 TE, up to 2 if using FLEX
        if self.use_double_te:
            self.problem += plp.lpSum(lp_variables[self.player_dict[(player, pos_str, team)]['ID']]
                                        for (player, pos_str, team) in self.player_dict if 'TE' == self.player_dict[(player, pos_str, team)]['Position']) >= 1, f'TE >= 1'
            self.problem += plp.lpSum(lp_variables[self.player_dict[(player, pos_str, team)]['ID']]
                                        for (player, pos_str, team) in self.player_dict if 'TE' == self.player_dict[(player, pos_str, team)]['Position']) <= 2, f'TE <= 2'
        else:
            self.problem += plp.lpSum(lp_variables[self.player_dict[(player, pos_str, team)]['ID']]
                                        for (player, pos_str, team) in self.player_dict if 'TE' == self.player_dict[(player, pos_str, team)]['Position']) == 1, f'TE == 1'

        # Need exactly 1 DST
        self.problem += plp.lpSum(lp_variables[self.player_dict[(player, pos_str, team)]['ID']]
                                    for (player, pos_str, team) in self.player_dict if 'DST' == self.player_dict[(player, pos_str, team)]['Position']) == 1, f'DST == 1'

        # Can only roster 9 total players
        self.problem += plp.lpSum(lp_variables[self.player_dict[(player, pos_str, team)]['ID']] for (player, pos_str, team) in self.player_dict) == 9, f'Total Players == 9'


        # Crunch!
        # for k in self.player_dict:
        #     print(k, self.player_dict[k]['Position'])
        for i in range(self.num_lineups):
            try:
                self.problem.solve(plp.PULP_CBC_CMD(msg=0))
            except plp.PulpSolverError:
                print('Infeasibility reached - only generated {} lineups out of {}. Continuing with export.'.format(
                    len(self.num_lineups), self.num_lineups))

            # Get the lineup and add it to our list
            self.problem.writeLP('file.lp')
            player_ids = [player for player in lp_variables if lp_variables[player].varValue != 0]
            print(player_ids)
            players = []
            for key, value in self.player_dict.items():
                if value['ID'] in player_ids:
                    players.append(value)
            print([p['Name'] for p in players])
            fpts = sum([player['Fpts'] for player in players])
            print(fpts)
            quit()
            
            score = str(self.problem.objective)
            for v in self.problem.variables():
                score = score.replace(v.name, str(v.varValue))

            if i % 100 == 0:
                print(i)
            player_ids = [int(v.name) for v in self.problem.variables() if v.varValue != 0]
            print(player_ids)
            players=[]
            for key, value in self.player_dict.items():
                if value['ID'] in player_ids:
                    players.append(value)
            print([p['Name'] for p in players])
            
            
            fpts = eval(score)
            self.lineups[fpts] = players
            
            print(fpts, players)

            # Set a new random fpts projection within their distribution
            if self.randomness_amount != 0:
                self.problem += plp.lpSum(np.random.normal(self.player_dict[(player, pos_str, team)]['Fpts'],
                                                        (self.player_dict[(player, pos_str, team)]['StdDev'] * self.randomness_amount / 100))
                                        * lp_variables[self.player_dict[(player, pos_str, team)]['ID']] for (player, pos_str, team) in self.player_dict), 'Objective'
            else:
                self.problem += plp.lpSum(self.player_dict[(player, pos_str, team)]['Fpts'] * lp_variables[self.player_dict[(player, pos_str, team)]['ID']]
                                for (player, pos_str, team) in self.player_dict) <= (fpts - 0.001), 'Objective'

    def output(self):
        print('Lineups done generating. Outputting.')
        unique = {}
        for fpts, lineup in self.lineups.items():
            if lineup not in unique.values():
                unique[fpts] = lineup
        
        self.lineups = unique
        if self.num_uniques != 1:
            num_uniq_lineups = plp.OrderedDict(
                sorted(self.lineups.items(), reverse=False, key=lambda t: t[0]))
            self.lineups = {}
            for fpts, lineup in num_uniq_lineups.copy().items():
                temp_lineups = list(num_uniq_lineups.values())
                temp_lineups.remove(lineup)
                use_lineup = True
                for x in temp_lineups:
                    common_players = set(x) & set(lineup)
                    roster_size = 9 if self.site == 'dk' else 8
                    if (roster_size - len(common_players)) < self.num_uniques:
                        use_lineup = False
                        del num_uniq_lineups[fpts]
                        break
                if use_lineup:
                    self.lineups[fpts] = lineup

        self.format_lineups()

        out_path = os.path.join(os.path.dirname(
            __file__), '../output/{}_optimal_lineups.csv'.format(self.site))
        with open(out_path, 'w') as f:
            f.write(
                    'QB,RB,RB,WR,WR,WR,TE,FLEX,DST,Salary,Fpts Proj,Ceiling,Own. Product,STDDEV\n')
            for fpts, x in self.lineups.items():
                # print(id_sum, tple)
                salary = sum(
                    self.player_dict[player]['Salary'] for player in x)
                fpts_p = sum(
                    self.player_dict[player]['Fpts'] for player in x)
                own_p = np.prod(
                    [self.player_dict[player]['Ownership'] for player in x])
                ceil = sum([self.player_dict[player]
                            ['Ceiling'] for player in x])
                stddev = np.prod(
                    [self.player_dict[player]['StdDev'] for player in x])
                # print(sum(self.player_dict[player]['Ownership'] for player in x))
                lineup_str = '{} ({}),{} ({}),{} ({}),{} ({}),{} ({}),{} ({}),{} ({}),{} ({}),{} ({}),{},{},{},{},{}'.format(
                    x[0].replace(
                        '#', '-'), self.player_dict[x[0]]['ID'],
                    x[1].replace(
                        '#', '-'), self.player_dict[x[1]]['ID'],
                    x[2].replace(
                        '#', '-'), self.player_dict[x[2]]['ID'],
                    x[3].replace(
                        '#', '-'), self.player_dict[x[3]]['ID'],
                    x[4].replace(
                        '#', '-'), self.player_dict[x[4]]['ID'],
                    x[5].replace(
                        '#', '-'), self.player_dict[x[5]]['ID'],
                    x[6].replace(
                        '#', '-'), self.player_dict[x[6]]['ID'],
                    x[7].replace(
                        '#', '-'), self.player_dict[x[7]]['ID'],
                    x[8].replace(
                        '#', '-'), self.player_dict[x[8]]['ID'],
                    salary, round(
                        fpts_p, 2), ceil, own_p, stddev
                )
                f.write('%s\n' % lineup_str)
            
        print('Output done.')

    def format_lineups(self):
        if self.site == 'dk':
            dk_roster = [['QB'], ['RB'], ['RB'], ['WR'], ['WR'], [
                'WR'], ['TE'], ['RB', 'WR', 'TE'], ['DST']]
            temp = self.lineups.items()
            self.lineups = {}
            for fpts, lineup in temp:
                finalized = [None] * 9
                z = 0
                cond = False
                while None in finalized:
                    if cond:
                        break
                    indices = [0, 1, 2, 3, 4, 5, 6, 7, 8]
                    shuffle(indices)
                    for i in indices:
                        if finalized[i] is None:
                            eligible_players = []
                            for player in lineup:
                                if self.player_dict[player]['Position'] in dk_roster[i]:
                                    eligible_players.append(player)
                            selected = choice(eligible_players)
                            # if there is an eligible player for this position not already in the finalized roster
                            if any(player not in finalized for player in eligible_players):
                                while selected in finalized:
                                    selected = choice(eligible_players)
                                finalized[i] = selected
                            # this lineup combination is no longer feasible - retry
                            else:
                                z += 1
                                if z == 1000:
                                    cond = True
                                    break

                                shuffle(indices)
                                finalized = [None] * 9
                                break
                if not cond:
                    self.lineups[fpts] = finalized
        else:
            fd_roster = [['PG'], ['PG'], ['SG'], ['SG'], ['SF'], [
                'SF'], ['PF'], ['PF'], ['C']]
            temp = self.lineups.items()
            self.lineups = {}
            for fpts, lineup in temp:
                finalized = [None] * 9
                z = 0
                cond = False
                infeasible = False
                while None in finalized:
                    if cond:
                        break
                    indices = [0, 1, 2, 3, 4, 5, 6, 7, 8]
                    shuffle(indices)
                    for i in indices:
                        if finalized[i] is None:
                            eligible_players = []
                            for player in lineup:
                                if any(pos in fd_roster[i] for pos in self.player_dict[player]['Position']):
                                    eligible_players.append(player)
                            selected = choice(eligible_players)
                            # if there is an eligible player for this position not already in the finalized roster
                            if any(player not in finalized for player in eligible_players):
                                while selected in finalized:
                                    selected = choice(eligible_players)
                                finalized[i] = selected
                            # this lineup combination is no longer feasible - retry
                            else:
                                z += 1
                                if z == 1000:
                                    cond = True
                                    infeasible = True
                                    break

                                shuffle(indices)
                                finalized = [None] * 9
                                break
                if not cond and not infeasible:
                    self.lineups[fpts] = finalized
