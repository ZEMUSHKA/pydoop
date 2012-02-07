# BEGIN_COPYRIGHT
# END_COPYRIGHT

import pydoop.hdfs
import pydoop.hadut as hadut

import argparse
import os
import random
import sys
import tempfile



class PydoopScript(object):
	DefaultReduceTasksPerNode = 3

	pipes_template = """
import sys
import os
sys.path.insert(0, os.getcwd())

import pydoop.pipes
import %(module)s

class PydoopScriptMapper(pydoop.pipes.Mapper):
	def __init__(self, ctx):
		super(type(self), self).__init__(ctx)
		self.context = ctx

	def emit(self, k,v):
		self.context.emit(str(k), str(v))
		
	def map(self, ctx):
		%(module)s.%(map_fn)s(ctx.getInputValue(), self.emit)

class PydoopScriptReducer(pydoop.pipes.Reducer):
	def __init__(self, ctx):
		super(type(self), self).__init__(ctx)
		self.context = ctx

	def emit(self, k,v):
		self.context.emit(str(k), str(v))

	@staticmethod
	def iter(ctx):
		while ctx.nextValue():
			yield ctx.getInputValue()

	def reduce(self, ctx):
		key = ctx.getInputKey()
		%(module)s.%(reduce_fn)s(key, PydoopScriptReducer.iter(ctx), self.emit)

## main
sys.exit(pydoop.pipes.runTask( pydoop.pipes.Factory(PydoopScriptMapper, PydoopScriptReducer) ))
"""

	class Args(argparse.Namespace):
		def __init__(self):
			self.properties = {}

	class SetProperty(argparse.Action):
		"""
		Used with argparse to parse arguments setting property values.
		Creates an attribute 'property' in the results namespace containing
		all the property-value pairs read from the command line.
		"""
		def __call__(self, parser, namespace, value, option_string=None):
			name, v = value.split('=', 1)
			namespace.properties[name] = v


	def __init__(self):
		self.parser = argparse.ArgumentParser(description="Easy map-reduce scripting with Pydoop")
		self.parser.add_argument('module', metavar='MODULE', help='Python module file')
		self.parser.add_argument('input', metavar='INPUT', help='hdfs input path')
		self.parser.add_argument('output', metavar='OUTPUT', help='hdfs output path')
		self.parser.add_argument('-m', '--map-fn', metavar='NAME', default='mapper', 
				help="Name of map function within module (default: mapper)")
		self.parser.add_argument('-r', '--reduce-fn', metavar='NAME', default='reducer', 
				help="Name of reduce function within module (default: reducer)")
		self.parser.add_argument('-t', '--kv-separator', metavar='STR', default='\t', 
				help="Key-value separator string in final output (default: <tab> character)")
		self.parser.add_argument('--num-reducers', metavar='INT', type=int, dest="num_reducers",
				help="Number of reduce tasks. Specify 0 to only perform map phase (default: 3 * num task trackers).")
		self.parser.add_argument('-D', metavar="PROP=VALUE", action=type(self).SetProperty,
				help='Set a property value, such as -D mapred.compress.map.output=true')

		# set default properties
		self.properties = {
			'hadoop.pipes.java.recordreader': 'true',
			'hadoop.pipes.java.recordwriter': 'true',
			'mapred.create.symlink': 'yes',
			'bl.libhdfs.opts': '-Xmx48m'
		}

		self.hdfs = None
		self.options = None

	def parse_cmd_line(self):
		# we scan the command line first in case the user wants to
		self.options, self.left_over_args = self.parser.parse_known_args(namespace=type(self).Args())

		# set the job name.  Do it here so the user can override it
		self.properties['mapred.job.name'] = os.path.basename(self.options.module)

		# now collect the property values specified in the options and
		# copy them to properties
		for k,v in self.options.properties.iteritems():
			self.properties[k] = v

		# number of reduce tasks
		if self.options.num_reducers is None:
			n_red_tasks = type(self).DefaultReduceTasksPerNode * hadut.num_nodes()
		else:
			n_red_tasks = self.options.num_reducers

		self.properties['mapred.reduce.tasks'] = n_red_tasks
		self.properties['mapred.textoutputformat.separator'] = self.options.kv_separator

	def __write_pipes_script(self, fd):
		ld_path = os.environ.get('LD_LIBRARY_PATH', None)
		pypath = os.environ.get('PYTHONPATH', '')

		fd.write("#!/bin/bash\n")
		fd.write('""":"\n')
		if ld_path:
			fd.write('export LD_LIBRARY_PATH="%s" # Seal dir + LD_LIBRARY_PATH copied from the env where you ran %s\n' % (ld_path, sys.argv[0]))
		if pypath:
			fd.write('export PYTHONPATH="%s"\n' % pypath)
		fd.write('exec "%s" -u "$0" "$@"\n' % sys.executable)
		fd.write('":"""\n')

		template_args = { 
				'module': os.path.splitext(os.path.os.path.basename(self.options.module))[0],
				'map_fn' : self.options.map_fn,
				'reduce_fn': self.options.reduce_fn
				}
		fd.write(type(self).pipes_template % template_args)


	def __validate(self):
		# validate running arguments
		if not os.access(self.options.module, os.R_OK):
			raise RuntimeError("Can't read module file %s" % self.options.module)

		if not self.hdfs.exists(self.options.input):
			raise RuntimeError("Input directory %s doesn't exist." % self.options.input)

		if self.hdfs.exists(self.options.output):
			raise RuntimeError("Output directory %s already exists.  Please delete it or specify a different output directory." % self.options.output)

	def run(self):
		if self.options is None:
			raise RuntimeError("You must call parse_cmd_line before run")

		remote_bin_dir = tempfile.mktemp(prefix='pydoop_script_run_dir.', suffix=str(random.random()), dir='')
		remote_pipes_bin = os.path.join(remote_bin_dir, 'pipes_script')
		remote_module = os.path.join(remote_bin_dir, os.path.basename(self.options.module))
		try:
			self.hdfs = pydoop.hdfs.hdfs('default', 0)
			self.__validate()

			dist_cache_parameter = "%s#%s" % (remote_module, os.path.basename(remote_module))
			if self.properties.get('mapred.cache.files', ''):
				self.properties['mapred.cache.files'] += ',' + dist_cache_parameter
			else:
				self.properties['mapred.cache.files'] = dist_cache_parameter

			try:
				with self.hdfs.open_file(remote_pipes_bin, 'w') as script:
					self.__write_pipes_script(script)

				with self.hdfs.open_file(remote_module, 'w') as module:
					with open(self.options.module) as local_module:
						module.write(local_module.read())

				return hadut.run_pipes(remote_pipes_bin, self.options.input, self.options.output,
					properties=self.properties, args_list=self.left_over_args)
			finally:
				try:
					self.hdfs.delete(remote_bin_dir) # delete the temporary pipes script from HDFS
				except:
					sys.stderr.write("Error deleting the temporary pipes script %s from HDFS" % remote_bin_dir)
					## don't re-raise the exception.  We're on our way out
		finally:
			if self.hdfs:
				tmp = self.hdfs
				self.hdfs = None
				tmp.close()

##############################################
# main
##############################################

if __name__ == '__main__':
	script = PydoopScript()
	script.parse_cmd_line()
	sys.exit(script.run())