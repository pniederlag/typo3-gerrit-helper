#! /usr/bin/env python
'''
Created on 22.03.2012

@author: pn
'''
import argparse
import shlex
import json
import shutil
from subprocess import check_call, check_output, STDOUT
import tempfile
import re
from ConfigParser import SafeConfigParser
from string import Template

class Typo3GerritHelper():
    '''
    some helper to create git repos and gerrit projects from forge projects
    pretty much tied to the infrastructure of the TYPO3 project
    
    @requirements:
    
        * gerrit account with group membership 'admin'
        * ssh config  (user) for hosts: review.typo3.org, srv108.typo3.org
        * ssh tunel 3309:127.0.0.1:3306 onto srv108.typo3.org (mysql)
        * copy .secret.example.cfg to .secret.example.cfg and put in db settings for forge
    
    @todo:
    
        * make ssh and hosts configurable
        * manage the ssh tunnel
        * get secrets from forge server
        * add svn2git conversion
        * map the forge_identifier into the git path
        * review API/Language Implementation
        
    '''
    
    def __init__(self,args):
        '''
        Constructor
        '''
        self.git_remote_url = 'review.typo3.org'
        #self.git_remote_url = 'ssh:\/\/jugglepro@review.local:29418'
        review_host = 'review.typo3.org'
        #review_host = '-p 29418 jugglepro@review.local'
        self.ssh_cmd = 'ssh ' + review_host
    
            #self.forge_db_id will be set in get_check_forge_id
        self.forge_db_id = False
            #self.old_svn_path will be set somewhere below
        self.old_svn_path = False
        
        parser = SafeConfigParser()
            # .secret
        parser.read('.secret.cfg')
        self.forge_db = parser.get('forge', 'db')
        self.forge_user = parser.get('forge', 'user')
        self.forge_pw = parser.get('forge', 'pw')
            # config
        parser.read('config.cfg')
        self.interactive = parser.getboolean('config', 'interactive')
        if args.interactive_false:
            self.interactive = False
        self.debug = parser.get('config', 'debug') 

    def run(self,forge_identifier,git_path):
        self.forge_identifier = forge_identifier
        
            # strip '.git' from git_path in case it was set
        regex = re.compile('(.*)(\.git)',re.UNICODE)
        matches=regex.search(git_path)
        if matches:
            self.git_path = matches.group(1)
        else:
            self.git_path = git_path
        print self.git_path       
        
        self.tmp_dir = tempfile.mkdtemp(prefix='t3git-')
           
        self.get_check_forge_identifier()
        self.get_repository_in_forge()
        
            # start real work
        self.create_group()
        self.create_project()
        self.update_project_config()
        self.cleanup_svn_repo()
        self.update_repository_in_forge()
        
            # cleanup
        self.cleanup_tmpdir()
    
    def cleanup_tmpdir(self):
        default = "YES"
        if self.interactive == False:
            shutil.rmtree(self.tmp_dir)
        else:
            user_input = raw_input('# Do you want to cleanup the temp directory "{0}"? [{1}|no]: '.format(self.tmp_dir,default))
            if not user_input or user_input == default:   
                shutil.rmtree(self.tmp_dir)
            else:
                print '# need to cleanup "{0}" yourself'.format(self.tmp_dir)        
        
    def create_group(self):
        '''
        adding a proper group for the project into gerrit including adding the proper forge_project_id
        '''
        output = self.execute(self.ssh_cmd + ' gerrit gsql --format JSON -c \\\"select * from account_group_names where name=\\\'' + self.git_path + '\\\'\\\"')
        lines = output.splitlines()
        sql_stat = json.loads(lines[-1]) # last line has stats
        count = sql_stat['rowCount']
        if count == 0:
            print '# will create Group "' + self.git_path + '" in gerrit'
            self.execute(self.ssh_cmd + ' gerrit create-group --owner Administrators ' + self.git_path)
        elif count == 1:
            print '# Group "' + self.git_path + '" is already known to gerrit'
        else:
            raise Exception('# querying gerrit for the group "' + self.git_path + '" failed for an unknown reason')
        self.execute(self.ssh_cmd + ' gerrit gsql -c \\\"update account_group_names set forge_project_id=\\\'' +  self.forge_identifier + '\\\' where name=\\\'' + self.git_path + '\\\' limit 1\\\"')
    
    def get_check_forge_identifier(self):
        '''
        try to find the id of the project in forge database by looking up the provided forge_identifier
        we always do this and bail out in case this is not succesfull
        '''
        try:
            query = 'select id from projects where identifier=\'' + self.forge_identifier + '\''
            cmd =  'mysql -u ' + self.forge_user + ' -h 127.0.0.1 -P 3309 -p' + self.forge_pw + ' -e "' + query + '" ' + self.forge_db
            output = self.execute(cmd)
            lines = output.splitlines()
            self.forge_db_id = int(lines[1])
        except Exception as ex:
            msg = 'ERROR: project "' + self.forge_identifier + '" not found. pls check it is valid\n'
            msg += '#\n'
            msg += '# Did you setup an ssh tunnel for mysql on port 3309 to srv108?\n'
            msg += '# ssh -N -L 3309:127.0.0.1:3306 srv108\n'
            msg += '#'
            raise Exception(msg)
    
    def get_repository_in_forge(self):
        old_svn_path = False
        rep_id = False
        query = 'select id, url from repositories where project_id=' + str(self.forge_db_id) + ''
        try:
            output = self.execute(
                        'mysql' +
                        ' -u ' + self.forge_user +
                        ' -h 127.0.0.1' +
                        ' -P 3309' +
                        ' -p' + self.forge_pw +
                        ' -e "' + query + '"' +
                        ' ' + self.forge_db
                        )
            lines = output.splitlines()
            [rep_id, old_svn_path] = lines[1].split('\t')
        except Exception as ex:
            #db_id = False
            print '# no repository for "' + self.forge_identifier + '" found. creation of new repo in forge is not supported yet.'
            return
        # check wether we have any rep at all
        if not old_svn_path or not rep_id:
            print '# no attached repo found on forge'
            return
        # check wether we still have an svn rep
        if not old_svn_path.startswith('https://svn'):
            print '# attached repo "' +  old_svn_path + '" seems not to be svn, maybe already git?'
        elif old_svn_path.endswith('/'):
            self.old_svn_path = old_svn_path
        else:
            self.old_svn_path = old_svn_path + '/'
        self.forge_rep_id = rep_id
    
    def update_repository_in_forge(self):
        if not self.forge_rep_id:
            print '#'
            print '# can\'t update the repository in forge as it is unknown. probably get_repository in forge has not been run?'
            return
        
        query = 'update repositories set url=\'/var/git/repositories/' + self.git_path + '.git\' , root_url=\'/var/git/repositories/' + self.git_path + '.git\', type=\'Git\' where id=' +  self.forge_rep_id + ''
        output = self.confirm_execute(
                    'mysql' +
                    ' -u ' + self.forge_user +
                    ' -h 127.0.0.1' +
                    ' -P 3309' +
                    ' -p' + self.forge_pw +
                    ' -e "' + query + '"' +
                    ' ' + self.forge_db
                    )
        if output:
            lines = output.splitlines()
            # what to do with the result output?
      
    def cleanup_svn_repo(self):
        if not self.old_svn_path:
            print '#'
            print '# can\'t cleanup the svn as old_svn_path is not known. probabld update_set_repository in forge has not been run?'
            return
        new_git_url = 'http://git.typo3.org/' + self.git_path + '.git'
        svn_info = self.execute('svn info ' + self.old_svn_path)
        regex = re.compile('(Revision: )([0-9]+)',re.UNICODE)
        matches=regex.search(svn_info)
        if matches:
            rev_no = matches.group(2)
        else:
            raise Exception('can\'t find the latest svn revision for ' + self.old_svn_path)
        
        commit_msg = 'Moved to git ' + new_git_url + ''
        
        directories = self.execute('svn ls ' + self.old_svn_path)
        if directories:
            cmd = 'svn rm -m "' + commit_msg + '"'
            for obsolete_dir in directories.splitlines():
                cmd = cmd + ' ' + self.old_svn_path + obsolete_dir
            self.confirm_execute(cmd)
            
        readme_template = Template(open('README.removed.template', "r").read())
        readme_file = tempfile.NamedTemporaryFile(delete=True)
        readme_content = readme_template.substitute(new_git_url=new_git_url, svn_rev_no=rev_no, svn_old_path=self.old_svn_path)
        readme_file.writelines([readme_content])
        readme_file.seek(0) # rewind
        cmd = 'svn import -m "' + commit_msg + '" ' + readme_file.name + ' ' + self.old_svn_path + 'README'
        self.confirm_execute(cmd)  
        readme_file.close()
          
    
    def create_project(self):
        output = self.execute(self.ssh_cmd + ' gerrit ls-projects')
        lines = output.splitlines()
        try:
            found = lines.index(self.git_path)
            print '# project has already been created'
        except (ValueError, LookupError):
            print '# will create project now'
            self.execute(self.ssh_cmd + ' gerrit create-project --require-change-id ' + self.git_path)
        #  touch the git-daemon-export-ok file to allow git browsing
        cmd = 'ssh -t srv104 sudo touch "/var/git/repositories/' + self.git_path + '.git/git-daemon-export-ok"'
        self.execute(cmd, call_only=True)
    
    def execute(self, cmd, cwd=None, call_only=False):
        output = None
        args = shlex.split(cmd)
        print '$ ' + cmd
        if call_only == True:
            check_call(args=args, cwd=cwd)
        else:
            output = check_output(args=args, cwd=cwd, stderr=STDOUT)
        if output and self.debug == 'True':
            for line in output.splitlines():
                print line
            #print output
        return output
            
    def confirm_execute(self,cmd, cwd=None, call_only=False):
        output = None
        if self.interactive == False:
            output = self.execute(cmd, cwd, call_only)
        else:
            print '############################################################'
            print '# Need your confirmation for the next command to perform:'
            print cmd
            output = None
            default = "YES"
            user_input = raw_input("# Do you want to execute above command? [%s|no]: " % default)
            if not user_input or user_input == default:
                print '# will execute'
                print '############################################################'
                output = self.execute(cmd, cwd, call_only)
            else:
                print '# INFO: skipping execution as requested!'
                print '############################################################'
        
        return output
    
    def update_project_config(self):

        output=self.execute(self.ssh_cmd + ' gerrit gsql --format JSON -c \\\"select name,group_uuid from account_groups where name=\\\'' +  self.git_path + '\\\'\\\"')
        lines = output.splitlines()
        sql_result=json.loads(lines[0])
        group_uid=sql_result['columns']['group_uuid']
        
        # FIXME, unfortunatly git remotes with ssh/config use ':' as first separator, while andthing else needs '/' 
        #origin = self.git_remote_url + '/' + self.git_path + '.git'
        origin = self.git_remote_url + ':' + self.git_path + '.git'
        self.execute('git init', cwd=self.tmp_dir)
        self.execute('git remote add origin ' + origin, cwd=self.tmp_dir)
        self.execute('git fetch origin refs/meta/config:refs/remotes/origin/meta/config', cwd=self.tmp_dir)
        self.execute('git checkout meta/config', cwd=self.tmp_dir)
        self.execute('git config --file ' + self.tmp_dir + '/project.config "access.refs/heads/*.label-Code-Review" "-2..+2 group ' + self.git_path + '"', cwd=self.tmp_dir)
        self.execute('git config --file ' + self.tmp_dir + '/project.config "access.refs/heads/*.label-Verified" "-1..+2 group ' + self.git_path + '"', cwd=self.tmp_dir)
        self.execute('git config --file ' + self.tmp_dir + '/project.config "access.refs/heads/*.submit" "group ' + self.git_path + '"', cwd=self.tmp_dir)
        self.execute('git config --file ' + self.tmp_dir + '/project.config "access.refs/tags/*.pushTag" "group ' + self.git_path + '"', cwd=self.tmp_dir)
        group_lines=[
             '# UUID                                  \tGroup Name\n',
             '#\n',
             group_uid + '\t' + self.git_path + '\n',
             ]
        groups_file = open(self.tmp_dir + '/groups', "w")
        groups_file.writelines(group_lines)
        groups_file.close()
        self.execute('git add project.config groups', cwd=self.tmp_dir)
        diff = self.execute('git diff meta/config origin/meta/config', cwd=self.tmp_dir)
        if diff == '':
            print '# permissions dont need an update'
        else:
            print '# updating permissions to default'
            self.execute('git commit -m "Default Permissions"', cwd=self.tmp_dir)
            self.confirm_execute('git push origin meta/config:refs/meta/config', cwd=self.tmp_dir)
 

parser = argparse.ArgumentParser(
                                 description='''TYPO3 helper to create gerrit projects based on forge projects

    Example:  %(prog)s extension_foo TYPO3v4/Extensions/foo
''',
                            formatter_class=argparse.RawDescriptionHelpFormatter
                            )

parser.add_argument('forge_identifier', nargs='?',
                   help='identifier string of forge project')
parser.add_argument('git_path', nargs='?',
                   help='path inside git structure')
parser.add_argument('-f','--file', type=open,
                   help='file name with lines of fields forge_id and git_path, fields must be separated by <tab>')
parser.add_argument('-y','--interactive-false',
                   action='store_true',
                   help='unset interactive mode, overrides value from config.cfg')              

args = parser.parse_args()

gerrit_helper = Typo3GerritHelper(args)

if args.file:
    try:
        #project_file = open(args.file)
        failed_projects = []
        for line in args.file.read().splitlines():
            [forge_identifier,git_path] = line.split('\t')
            #print forge_id + ' ' + git_path
            try:
                print '##############################################'
                print '# start processing "{0}" "{1}"'.format(forge_identifier, git_path)
                print '##############################################'
                gerrit_helper.run(forge_identifier, git_path)
            except Exception as ex:
                print ''
                print '############################################################################'
                print '# ERROR: project "{0}" "{1}" could not processed'.format(forge_identifier, git_path)
                print '#'
                print '# {0}'.format(ex)
                print '############################################################################'
                print ''
                failed_projects.append(forge_identifier)
        if failed_projects:
            print ''
            print '!!!!!! projects that failed: !!!!!!!'
            print failed_projects
    except Exception as ex:
        print ''
        print '############################################################################'
        print '# ERROR: could not read/iterate --file={0}'.format(args.file)
        print '#        fields should be terminated by <tab>'
        print '#'
        print '# {0}'.format(ex)
        print '############################################################################'
        print ''
        parser.print_help()
elif (args.forge_identifier != None and args.git_path != None):
    try:
        forge_identifier = args.forge_identifier
        git_path = args.git_path
        print '##############################################'
        print '# start processing "{0}" "{1}"'.format(forge_identifier, git_path)
        print '##############################################'    
        gerrit_helper.run(forge_identifier, git_path)
    except Exception as ex:
        print ''
        print '############################################################################'
        print '# ERROR: project "{0}" "{1}" could not processed'.format(forge_identifier, git_path)
        print '#'
        print '# {0}'.format(ex)
        print '############################################################################'
        print ''
else:
    print '############################################################################'
    print '# ERROR: you must either provide forge_identifier and git_id or --file=SomeFile'
    print '#'
    print '############################################################################'
    print ''
    parser.print_help()

