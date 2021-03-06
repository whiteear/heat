#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import copy
import json

import mox
from oslo.config import cfg
import six
from testtools import matchers

from heat.common import exception
from heat.common import template_format
from heat.engine.clients.os import nova
from heat.engine import function
from heat.engine.notification import stack as notification
from heat.engine import parser
from heat.engine.resources.aws import wait_condition_handle as aws_wch
from heat.engine.resources import instance
from heat.engine.resources import loadbalancer as lb
from heat.tests import common
from heat.tests import utils
from heat.tests.v1_1 import fakes as fakes_v1_1


asg_tmpl_without_updt_policy = '''
{
  "AWSTemplateFormatVersion" : "2010-09-09",
  "Description" : "Template to create autoscaling group.",
  "Parameters" : {},
  "Resources" : {
    "WebServerGroup" : {
      "Type" : "AWS::AutoScaling::AutoScalingGroup",
      "Properties" : {
        "AvailabilityZones" : ["nova"],
        "LaunchConfigurationName" : { "Ref" : "LaunchConfig" },
        "MinSize" : "10",
        "MaxSize" : "20",
        "LoadBalancerNames" : [ { "Ref" : "ElasticLoadBalancer" } ]
      }
    },
    "ElasticLoadBalancer" : {
        "Type" : "AWS::ElasticLoadBalancing::LoadBalancer",
        "Properties" : {
            "AvailabilityZones" : ["nova"],
            "Listeners" : [ {
                "LoadBalancerPort" : "80",
                "InstancePort" : "80",
                "Protocol" : "HTTP"
            }]
        }
    },
    "LaunchConfig" : {
      "Type" : "AWS::AutoScaling::LaunchConfiguration",
      "Properties": {
        "ImageId"           : "F20-x86_64-cfntools",
        "InstanceType"      : "m1.medium",
        "KeyName"           : "test",
        "SecurityGroups"    : [ "sg-1" ],
        "UserData"          : "jsconfig data"
      }
    }
  }
}
'''

asg_tmpl_with_bad_updt_policy = '''
{
  "AWSTemplateFormatVersion" : "2010-09-09",
  "Description" : "Template to create autoscaling group.",
  "Parameters" : {},
  "Resources" : {
    "WebServerGroup" : {
      "UpdatePolicy": {
        "foo": {
        }
      },
      "Type" : "AWS::AutoScaling::AutoScalingGroup",
      "Properties" : {
        "AvailabilityZones" : ["nova"],
        "LaunchConfigurationName" : { "Ref" : "LaunchConfig" },
        "MinSize" : "10",
        "MaxSize" : "20"
      }
    },
    "LaunchConfig" : {
      "Type" : "AWS::AutoScaling::LaunchConfiguration",
      "Properties": {
        "ImageId"           : "F20-x86_64-cfntools",
        "InstanceType"      : "m1.medium",
        "KeyName"           : "test",
        "SecurityGroups"    : [ "sg-1" ],
        "UserData"          : "jsconfig data"
      }
    }
  }
}
'''

asg_tmpl_with_default_updt_policy = '''
{
  "AWSTemplateFormatVersion" : "2010-09-09",
  "Description" : "Template to create autoscaling group.",
  "Parameters" : {},
  "Resources" : {
    "WebServerGroup" : {
      "UpdatePolicy" : {
        "AutoScalingRollingUpdate" : {
        }
      },
      "Type" : "AWS::AutoScaling::AutoScalingGroup",
      "Properties" : {
        "AvailabilityZones" : ["nova"],
        "LaunchConfigurationName" : { "Ref" : "LaunchConfig" },
        "MinSize" : "10",
        "MaxSize" : "20",
        "LoadBalancerNames" : [ { "Ref" : "ElasticLoadBalancer" } ]
      }
    },
    "ElasticLoadBalancer" : {
        "Type" : "AWS::ElasticLoadBalancing::LoadBalancer",
        "Properties" : {
            "AvailabilityZones" : ["nova"],
            "Listeners" : [ {
                "LoadBalancerPort" : "80",
                "InstancePort" : "80",
                "Protocol" : "HTTP"
            }]
        }
    },
    "LaunchConfig" : {
      "Type" : "AWS::AutoScaling::LaunchConfiguration",
      "Properties": {
        "ImageId"           : "F20-x86_64-cfntools",
        "InstanceType"      : "m1.medium",
        "KeyName"           : "test",
        "SecurityGroups"    : [ "sg-1" ],
        "UserData"          : "jsconfig data"
      }
    }
  }
}
'''

asg_tmpl_with_updt_policy = '''
{
  "AWSTemplateFormatVersion" : "2010-09-09",
  "Description" : "Template to create autoscaling group.",
  "Parameters" : {},
  "Resources" : {
    "WebServerGroup" : {
      "UpdatePolicy" : {
        "AutoScalingRollingUpdate" : {
          "MinInstancesInService" : "1",
          "MaxBatchSize" : "2",
          "PauseTime" : "PT1S"
        }
      },
      "Type" : "AWS::AutoScaling::AutoScalingGroup",
      "Properties" : {
        "AvailabilityZones" : ["nova"],
        "LaunchConfigurationName" : { "Ref" : "LaunchConfig" },
        "MinSize" : "10",
        "MaxSize" : "20",
        "LoadBalancerNames" : [ { "Ref" : "ElasticLoadBalancer" } ]
      }
    },
    "ElasticLoadBalancer" : {
        "Type" : "AWS::ElasticLoadBalancing::LoadBalancer",
        "Properties" : {
            "AvailabilityZones" : ["nova"],
            "Listeners" : [ {
                "LoadBalancerPort" : "80",
                "InstancePort" : "80",
                "Protocol" : "HTTP"
            }]
        }
    },
    "LaunchConfig" : {
      "Type" : "AWS::AutoScaling::LaunchConfiguration",
      "Properties": {
        "ImageId"           : "F20-x86_64-cfntools",
        "InstanceType"      : "m1.medium",
        "KeyName"           : "test",
        "SecurityGroups"    : [ "sg-1" ],
        "UserData"          : "jsconfig data"
      }
    }
  }
}
'''


class AutoScalingGroupTest(common.HeatTestCase):

    def setUp(self):
        super(AutoScalingGroupTest, self).setUp()
        self.fc = fakes_v1_1.FakeClient()
        self.stub_keystoneclient(username='test_stack.CfnLBUser')
        cfg.CONF.set_default('heat_waitcondition_server_url',
                             'http://127.0.0.1:8000/v1/waitcondition')

    def _stub_validate(self):
        self.m.StubOutWithMock(parser.Stack, 'validate')
        parser.Stack.validate().MultipleTimes()

    def _stub_lb_create(self):
        self.m.StubOutWithMock(aws_wch.WaitConditionHandle, 'get_status')
        aws_wch.WaitConditionHandle.get_status().AndReturn(['SUCCESS'])

    def _stub_lb_reload(self, num=1, setup=True):
        if setup:
            self.m.StubOutWithMock(lb.LoadBalancer, 'handle_update')
        for i in range(num):
            lb.LoadBalancer.handle_update(
                mox.IgnoreArg(), mox.IgnoreArg(),
                mox.IgnoreArg()).AndReturn(None)

    def _stub_grp_create(self, capacity=0, setup_lb=True):
        """
        Expect creation of instances to capacity. By default, expect creation
        of load balancer unless specified.
        """
        self._stub_validate()

        self.m.StubOutWithMock(instance.Instance, 'handle_create')
        self.m.StubOutWithMock(instance.Instance, 'check_create_complete')

        self.m.StubOutWithMock(notification, 'send')
        notification.send(mox.IgnoreArg()).MultipleTimes().AndReturn(None)

        cookie = object()

        self.m.StubOutWithMock(nova.NovaClientPlugin, '_create')
        nova.NovaClientPlugin._create().AndReturn(self.fc)
        # for load balancer setup
        if setup_lb:
            self._stub_lb_create()
            self._stub_lb_reload()
            instance.Instance.handle_create().AndReturn(cookie)
            instance.Instance.check_create_complete(cookie).AndReturn(True)

        # for each instance in group
        for i in range(capacity):
            instance.Instance.handle_create().AndReturn(cookie)
            instance.Instance.check_create_complete(cookie).AndReturn(True)

    def _stub_grp_replace(self,
                          num_creates_expected_on_updt=0,
                          num_deletes_expected_on_updt=0,
                          num_reloads_expected_on_updt=0):
        """
        Expect replacement of the capacity by batch size
        """
        # for load balancer setup
        self._stub_lb_reload(num_reloads_expected_on_updt)

        self.m.StubOutWithMock(notification, 'send')
        notification.send(mox.IgnoreArg()).MultipleTimes().AndReturn(None)

        # for instances in the group
        self.m.StubOutWithMock(instance.Instance, 'handle_create')
        self.m.StubOutWithMock(instance.Instance, 'check_create_complete')
        self.m.StubOutWithMock(instance.Instance, 'destroy')

        cookie = object()
        for i in range(num_creates_expected_on_updt):
            instance.Instance.handle_create().AndReturn(cookie)
            instance.Instance.check_create_complete(cookie).AndReturn(True)
        for i in range(num_deletes_expected_on_updt):
            instance.Instance.destroy().AndReturn(None)

    def _stub_grp_update(self,
                         num_creates_expected_on_updt=0,
                         num_deletes_expected_on_updt=0,
                         num_reloads_expected_on_updt=0):
        """
        Expect update of the instances
        """

        def activate_status(server):
            server.status = 'VERIFY_RESIZE'

        return_server = self.fc.servers.list()[1]
        return_server.id = '1234'
        return_server.get = activate_status.__get__(return_server)

        self.m.StubOutWithMock(self.fc.servers, 'get')
        self.m.StubOutWithMock(self.fc.client, 'post_servers_1234_action')

        self.fc.servers.get(
            mox.IgnoreArg()).MultipleTimes().AndReturn(return_server)
        self.fc.client.post_servers_1234_action(
            body={'resize': {'flavorRef': 3}}
        ).MultipleTimes().AndReturn((202, None))
        self.fc.client.post_servers_1234_action(
            body={'confirmResize': None}
        ).MultipleTimes().AndReturn((202, None))

        self._stub_grp_replace(num_creates_expected_on_updt,
                               num_deletes_expected_on_updt,
                               num_reloads_expected_on_updt)

    def get_launch_conf_name(self, stack, ig_name):
        return stack[ig_name].properties['LaunchConfigurationName']

    def test_parse_without_update_policy(self):
        tmpl = template_format.parse(asg_tmpl_without_updt_policy)
        stack = utils.parse_stack(tmpl)
        self.stub_ImageConstraint_validate()
        self.stub_KeypairConstraint_validate()
        self.stub_FlavorConstraint_validate()
        self.m.ReplayAll()

        stack.validate()
        grp = stack['WebServerGroup']
        self.assertFalse(grp.update_policy['AutoScalingRollingUpdate'])
        self.m.VerifyAll()

    def test_parse_with_update_policy(self):
        tmpl = template_format.parse(asg_tmpl_with_updt_policy)
        stack = utils.parse_stack(tmpl)
        self.stub_ImageConstraint_validate()
        self.stub_KeypairConstraint_validate()
        self.stub_FlavorConstraint_validate()
        self.m.ReplayAll()

        stack.validate()
        tmpl_grp = tmpl['Resources']['WebServerGroup']
        tmpl_policy = tmpl_grp['UpdatePolicy']['AutoScalingRollingUpdate']
        tmpl_batch_sz = int(tmpl_policy['MaxBatchSize'])
        grp = stack['WebServerGroup']
        self.assertTrue(grp.update_policy)
        self.assertEqual(1, len(grp.update_policy))
        self.assertIn('AutoScalingRollingUpdate', grp.update_policy)
        policy = grp.update_policy['AutoScalingRollingUpdate']
        self.assertTrue(policy and len(policy) > 0)
        self.assertEqual(1, int(policy['MinInstancesInService']))
        self.assertEqual(tmpl_batch_sz, int(policy['MaxBatchSize']))
        self.assertEqual('PT1S', policy['PauseTime'])
        self.m.VerifyAll()

    def test_parse_with_default_update_policy(self):
        tmpl = template_format.parse(asg_tmpl_with_default_updt_policy)
        stack = utils.parse_stack(tmpl)
        self.stub_ImageConstraint_validate()
        self.stub_KeypairConstraint_validate()
        self.stub_FlavorConstraint_validate()
        self.m.ReplayAll()

        stack.validate()
        grp = stack['WebServerGroup']
        self.assertTrue(grp.update_policy)
        self.assertEqual(1, len(grp.update_policy))
        self.assertIn('AutoScalingRollingUpdate', grp.update_policy)
        policy = grp.update_policy['AutoScalingRollingUpdate']
        self.assertTrue(policy and len(policy) > 0)
        self.assertEqual(0, int(policy['MinInstancesInService']))
        self.assertEqual(1, int(policy['MaxBatchSize']))
        self.assertEqual('PT0S', policy['PauseTime'])
        self.m.VerifyAll()

    def test_parse_with_bad_update_policy(self):
        self.stub_ImageConstraint_validate()
        self.stub_KeypairConstraint_validate()
        self.stub_FlavorConstraint_validate()
        self.m.ReplayAll()
        tmpl = template_format.parse(asg_tmpl_with_bad_updt_policy)
        stack = utils.parse_stack(tmpl)
        error = self.assertRaises(
            exception.StackValidationFailed, stack.validate)
        self.assertIn("foo", six.text_type(error))

    def test_parse_with_bad_pausetime_in_update_policy(self):
        self.stub_ImageConstraint_validate()
        self.stub_KeypairConstraint_validate()
        self.stub_FlavorConstraint_validate()
        self.m.ReplayAll()
        tmpl = template_format.parse(asg_tmpl_with_default_updt_policy)
        group = tmpl['Resources']['WebServerGroup']
        policy = group['UpdatePolicy']['AutoScalingRollingUpdate']
        policy['PauseTime'] = 'P1YT1H'
        stack = utils.parse_stack(tmpl)
        error = self.assertRaises(
            exception.StackValidationFailed, stack.validate)
        self.assertIn("Only ISO 8601 duration format", six.text_type(error))

    def validate_update_policy_diff(self, current, updated):

        # load current stack
        current_tmpl = template_format.parse(current)
        current_stack = utils.parse_stack(current_tmpl)

        # get the json snippet for the current InstanceGroup resource
        current_grp = current_stack['WebServerGroup']
        current_snippets = dict((n, r.parsed_template())
                                for n, r in current_stack.items())
        current_grp_json = current_snippets[current_grp.name]

        # load the updated stack
        updated_tmpl = template_format.parse(updated)
        updated_stack = utils.parse_stack(updated_tmpl)

        # get the updated json snippet for the InstanceGroup resource in the
        # context of the current stack
        updated_grp = updated_stack['WebServerGroup']
        updated_grp_json = function.resolve(updated_grp.t)

        # identify the template difference
        tmpl_diff = updated_grp.update_template_diff(
            updated_grp_json, current_grp_json)
        updated_policy = (updated_grp.t['UpdatePolicy']
                          if 'UpdatePolicy' in updated_grp.t else None)
        expected = {u'UpdatePolicy': updated_policy}
        self.assertEqual(expected, tmpl_diff)

    def test_update_policy_added(self):
        self.validate_update_policy_diff(asg_tmpl_without_updt_policy,
                                         asg_tmpl_with_updt_policy)

    def test_update_policy_updated(self):
        updt_template = json.loads(asg_tmpl_with_updt_policy)
        grp = updt_template['Resources']['WebServerGroup']
        policy = grp['UpdatePolicy']['AutoScalingRollingUpdate']
        policy['MinInstancesInService'] = '2'
        policy['MaxBatchSize'] = '4'
        policy['PauseTime'] = 'PT1M30S'
        self.validate_update_policy_diff(asg_tmpl_with_updt_policy,
                                         json.dumps(updt_template))

    def test_update_policy_removed(self):
        self.validate_update_policy_diff(asg_tmpl_with_updt_policy,
                                         asg_tmpl_without_updt_policy)

    def test_autoscaling_group_update_policy_removed(self):

        # setup stack from the initial template
        tmpl = template_format.parse(asg_tmpl_with_updt_policy)
        stack = utils.parse_stack(tmpl)
        self.stub_ImageConstraint_validate()
        self.stub_KeypairConstraint_validate()
        self.stub_FlavorConstraint_validate()
        self.m.ReplayAll()

        stack.validate()
        self.m.VerifyAll()
        self.m.UnsetStubs()

        # test stack create
        size = int(stack['WebServerGroup'].properties['MinSize'])
        self._stub_grp_create(size)
        self.stub_ImageConstraint_validate()
        self.stub_FlavorConstraint_validate()
        self.m.ReplayAll()
        stack.create()
        self.m.VerifyAll()
        self.assertEqual(('CREATE', 'COMPLETE'), stack.state)

        # test that update policy is loaded
        current_grp = stack['WebServerGroup']
        self.assertIn('AutoScalingRollingUpdate', current_grp.update_policy)
        current_policy = current_grp.update_policy['AutoScalingRollingUpdate']
        self.assertTrue(current_policy)
        self.assertTrue(len(current_policy) > 0)
        init_updt_policy = tmpl['Resources']['WebServerGroup']['UpdatePolicy']
        init_roll_updt = init_updt_policy['AutoScalingRollingUpdate']
        init_batch_sz = int(init_roll_updt['MaxBatchSize'])
        self.assertEqual(init_batch_sz, int(current_policy['MaxBatchSize']))

        # test that physical resource name of launch configuration is used
        conf = stack['LaunchConfig']
        conf_name_pattern = '%s-LaunchConfig-[a-zA-Z0-9]+$' % stack.name
        self.assertThat(conf.FnGetRefId(),
                        matchers.MatchesRegex(conf_name_pattern))

        # test the number of instances created
        nested = stack['WebServerGroup'].nested()
        self.assertEqual(size, len(nested.resources))

        # clean up for next test
        self.m.UnsetStubs()

        # test stack update
        updated_tmpl = template_format.parse(asg_tmpl_without_updt_policy)
        updated_stack = utils.parse_stack(updated_tmpl)
        self._stub_grp_replace(num_creates_expected_on_updt=0,
                               num_deletes_expected_on_updt=0,
                               num_reloads_expected_on_updt=1)
        self.m.ReplayAll()
        stack.update(updated_stack)
        self.m.VerifyAll()
        self.assertEqual(('UPDATE', 'COMPLETE'), stack.state)

        # test that update policy is removed
        updated_grp = stack['WebServerGroup']
        self.assertFalse(updated_grp.update_policy['AutoScalingRollingUpdate'])

    def test_autoscaling_group_update_policy_check_timeout(self):

        # setup stack from the initial template
        tmpl = template_format.parse(asg_tmpl_with_updt_policy)
        stack = utils.parse_stack(tmpl)

        # test stack create
        size = int(stack['WebServerGroup'].properties['MinSize'])
        self._stub_grp_create(size)
        self.stub_ImageConstraint_validate()
        self.stub_FlavorConstraint_validate()
        self.m.ReplayAll()
        stack.create()
        self.m.VerifyAll()
        self.assertEqual(('CREATE', 'COMPLETE'), stack.state)

        # test that update policy is loaded
        current_grp = stack['WebServerGroup']
        self.assertIn('AutoScalingRollingUpdate', current_grp.update_policy)
        current_policy = current_grp.update_policy['AutoScalingRollingUpdate']
        self.assertTrue(current_policy)
        self.assertTrue(len(current_policy) > 0)
        init_updt_policy = tmpl['Resources']['WebServerGroup']['UpdatePolicy']
        init_roll_updt = init_updt_policy['AutoScalingRollingUpdate']
        init_batch_sz = int(init_roll_updt['MaxBatchSize'])
        self.assertEqual(init_batch_sz, int(current_policy['MaxBatchSize']))

        # test the number of instances created
        nested = stack['WebServerGroup'].nested()
        self.assertEqual(size, len(nested.resources))

        # clean up for next test
        self.m.UnsetStubs()

        # modify the pause time and test for error
        # the following test, effective_capacity is 12
        # batch_count = (effective_capacity + batch_size -1)//batch_size
        # = (12 + 2 - 1)//2 = 6
        # if (batch_count - 1)* pause_time > stack.time_out, to raise error
        # (6 - 1)*14*60 > 3600, so to raise error
        new_pause_time = 'PT14M'
        min_in_service = 10
        updt_template = json.loads(copy.deepcopy(asg_tmpl_with_updt_policy))
        group = updt_template['Resources']['WebServerGroup']
        policy = group['UpdatePolicy']['AutoScalingRollingUpdate']
        policy['PauseTime'] = new_pause_time
        policy['MinInstancesInService'] = min_in_service
        config = updt_template['Resources']['LaunchConfig']
        config['Properties']['ImageId'] = 'F17-x86_64-cfntools'
        updated_tmpl = template_format.parse(json.dumps(updt_template))
        updated_stack = utils.parse_stack(updated_tmpl)

        self.stub_KeypairConstraint_validate()
        self.stub_ImageConstraint_validate()
        self.stub_FlavorConstraint_validate()
        self.m.ReplayAll()
        stack.update(updated_stack)
        self.m.VerifyAll()
        self.assertEqual(('UPDATE', 'FAILED'), stack.state)

        # test that the update policy is updated
        updated_grp = stack['WebServerGroup']
        self.assertIn('AutoScalingRollingUpdate', updated_grp.update_policy)
        updated_policy = updated_grp.update_policy['AutoScalingRollingUpdate']
        self.assertTrue(updated_policy)
        self.assertTrue(len(updated_policy) > 0)
        self.assertEqual(new_pause_time, updated_policy['PauseTime'])

        # test that error message match
        expected_error_message = ('The current UpdatePolicy will result '
                                  'in stack update timeout.')
        self.assertIn(expected_error_message, stack.status_reason)
