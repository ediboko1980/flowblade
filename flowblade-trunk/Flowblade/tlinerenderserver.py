"""
    Flowblade Movie Editor is a nonlinear video editor.
    Copyright 2012 Janne Liljeblad.

    This file is part of Flowblade Movie Editor <http://code.google.com/p/flowblade>.

    Flowblade Movie Editor is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    Flowblade Movie Editor is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with Flowblade Movie Editor. If not, see <http://www.gnu.org/licenses/>.
"""

from gi.repository import GLib
    
import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
import locale
import mlt
import subprocess
import sys
import threading
import time

import editorstate
import mltenv
import mlttransitions
import mltfilters
import mltprofiles
import editorpersistance
import processutils
import renderconsumer
import respaths
import translations
import userfolders


TLINE_RENDER_ENCODING_INDEX = 0
RENDERING_PAD_FRAMES = 3

_dbus_service = None


# --------------------------------------------------------------- interface
def launch_render_server():
    bus = dbus.SessionBus()
    if bus.name_has_owner('flowblade.movie.editor.tlinerenderserver'):
        # This should not be happening, we should be deleting session files and close service oon peoject chages. 
        print("NOTE !!!: flowblade.movie.editor.tlinerenderserver dbus service exists ON LAUNCH REQUEST")
        return False
    else:
        FLOG = open(userfolders.get_cache_dir() + "log_tline_render", 'w')
        subprocess.Popen([sys.executable, respaths.LAUNCH_DIR + "flowbladetlinerender"], stdin=FLOG, stdout=FLOG, stderr=FLOG)
        return True

def render_update_clips(sequence_xml_path, segments_paths, segments_ins, segments_outs, profile_name):
    iface = _get_iface("render_update_clips")
    if iface != None:
        iface.render_update_clips(sequence_xml_path, segments_paths, segments_ins, segments_outs, profile_name)

def get_render_status():
    iface = _get_iface("get_render_status")
    if iface != None:
        return iface.get_render_status()

def abort_current_renders():
    iface = _get_iface("abort_current_renders")
    if iface != None:
        iface.abort_renders()
        
def shutdown_render_server():
    iface = _get_iface("shutdown_render_server")
    if iface != None:
        iface.shutdown_render_server()

def get_encoding_extension():
    return renderconsumer.proxy_encodings[TLINE_RENDER_ENCODING_INDEX].extension

def _get_iface(method_name):
    bus = dbus.SessionBus()
    if bus.name_has_owner('flowblade.movie.editor.tlinerenderserver'):
        obj = bus.get_object('flowblade.movie.editor.tlinerenderserver', '/flowblade/movie/editor/tlinerenderserver')
        iface = dbus.Interface(obj, 'flowblade.movie.editor.tlinerenderserver')
        return iface
    else:
        print("Timeline background render service not available on DBus at", method_name)
        return None


# ---------------------------------------------------------------- server

def main(root_path, force_launch=False):
    try:
        editorstate.mlt_version = mlt.LIBMLT_VERSION
    except:
        editorstate.mlt_version = "0.0.99" # magic string for "not found"

    # Get XDG paths etc.
    userfolders.init()
    
    # Set paths.
    respaths.set_paths(root_path)

    # Load editor prefs and list of recent projects
    editorpersistance.load()
    
    # Init translations module with translations data
    translations.init_languages()
    translations.load_filters_translations()
    mlttransitions.init_module()
       
    editorpersistance.load()

    repo = mlt.Factory().init()
    processutils.prepare_mlt_repo(repo)
    
    # Set numeric locale to use "." as radix, MLT initilizes this to OS locale and this causes bugs 
    locale.setlocale(locale.LC_NUMERIC, 'C')

    # Check for codecs and formats on the system
    mltenv.check_available_features(repo)
    renderconsumer.load_render_profiles()

    # Load filter and compositor descriptions from xml files.
    mltfilters.load_filters_xml(mltenv.services)
    mlttransitions.load_compositors_xml(mltenv.transitions)

    # Create list of available mlt profiles
    mltprofiles.load_profile_list()

    # Launch server
    DBusGMainLoop(set_as_default=True)
    loop = GLib.MainLoop()
    global _dbus_service
    _dbus_service = TLineRenderDBUSService(loop)
    print("tline render service running")
    loop.run()

def _get_render_encoding():
    return renderconsumer.proxy_encodings[TLINE_RENDER_ENCODING_INDEX]




class TLineRenderDBUSService(dbus.service.Object):
    def __init__(self, loop):
        bus_name = dbus.service.BusName('flowblade.movie.editor.tlinerenderserver', bus=dbus.SessionBus())
        dbus.service.Object.__init__(self, bus_name, '/flowblade/movie/editor/tlinerenderserver')
        self.main_loop = loop

        self.render_runner_thread = None
        
    @dbus.service.method('flowblade.movie.editor.tlinerenderserver')
    def render_update_clips(self, sequence_xml_path, segments_paths, segments_ins, segments_outs, profile_name):
        print(sequence_xml_path, profile_name)
        
        segments = []
        for i in range(0, len(segments_paths)):
            clip_path = segments_paths[i]
            clip_range_in = segments_ins[i]
            clip_range_out = segments_outs[i]
            segments.append((clip_path, clip_range_in, clip_range_out))
            
            print("render segment:", clip_path, clip_range_in, clip_range_out)

        self.render_runner_thread = TLineRenderRunnerThread(self, sequence_xml_path, segments, profile_name)
        self.render_runner_thread.start()

    @dbus.service.method('flowblade.movie.editor.tlinerenderserver')
    def get_render_status(self):
        dummy_list = ["nothing"]
        if self.render_runner_thread == None:
            return ("none", 1.0,  False, dummy_list)
        
        if self.render_runner_thread.render_complete:
            return ("none", 1.0, self.render_runner_thread.render_complete, self.render_runner_thread.completed_segments)
        
        return ( self.render_runner_thread.current_render_file_path, self.render_runner_thread.get_fraction(), 
                  self.render_runner_thread.render_complete, self.render_runner_thread.completed_segments)

    @dbus.service.method('flowblade.movie.editor.tlinerenderserver')
    def abort_renders(self):
        # not impl
        return

    @dbus.service.method('flowblade.movie.editor.tlinerenderserver')
    def shutdown_render_server(self):
        self.remove_from_connection()
        self.main_loop.quit()




class TLineRenderRunnerThread(threading.Thread):
    """
    SINGLE THREADED RENDERING, SHOULD WE GET MULTIPLE PROCESSES GOING FOR MULTIPLE CLIPS LATER IN MODERN MULTICORE MACHINES?
    """
    def __init__(self, dbus_service, sequence_xml_path, segments, profile_name):
        threading.Thread.__init__(self)
        
        self.dbus_service = dbus_service
        self.sequence_xml_path = sequence_xml_path
        self.profile = mltprofiles.get_profile(profile_name)
        self.segments = segments
        self.completed_segments =  ["nothing"]
        self.render_complete = False
        self.render_thread = None

        self.aborted = False

    def run(self):        

        width, height = self.profile.width(), self.profile.height()
        encoding = _get_render_encoding()
        self.current_render_file_path = None
        
        sequence_xml_producer = mlt.Producer(self.profile, str(self.sequence_xml_path))
        
        for segment in self.segments:
            if self.aborted == True:
                break
                
            clip_file_path, clip_range_in, clip_range_out = segment

            # Create render objects
            self.current_render_file_path = clip_file_path
            renderconsumer.performance_settings_enabled = False
            
            consumer = renderconsumer.get_render_consumer_for_encoding( clip_file_path,
                                                                        self.profile, 
                                                                        encoding)
            renderconsumer.performance_settings_enabled = True
            
            # DIS STUFF FROM PROXY RENDERING, REVISIT!
            # Bit rates for proxy files are counted using 2500kbs for 
            # PAL size image as starting point.
            pal_pix_count = 720.0 * 576.0
            pal_proxy_rate = 2500.0
            proxy_pix_count = float(width * height)
            proxy_rate = pal_proxy_rate * (proxy_pix_count / pal_pix_count)
            proxy_rate = int(proxy_rate / 100) * 100 # Make proxy rate even hundred
            # There are no practical reasons to have bitrates lower than 500kbs.
            if proxy_rate < 500:
                proxy_rate = 500
            consumer.set("vb", str(int(proxy_rate)) + "k")

            consumer.set("rescale", "nearest")

            start_frame = clip_range_in 
            
            stop_frame = clip_range_out + RENDERING_PAD_FRAMES
            if stop_frame > sequence_xml_producer.get_length() - 1:
                stop_frame = sequence_xml_producer.get_length() - 1

            # Create and launch render thread
            self.render_thread = renderconsumer.FileRenderPlayer(None, sequence_xml_producer, consumer, start_frame, stop_frame)
            self.render_thread.wait_for_producer_end_stop = False
            self.render_thread.start()

            # Render view update loop
            self.render_in_progress = True
            self.aborted = False
            while self.render_in_progress:
                if self.aborted == True:
                    break

                if self.render_thread.running == False: # Rendering has reached end
                    self.render_in_progress = False
                    self.current_render_file_path = None
                else:
                    time.sleep(0.1)

            if self.aborted:
                self.render_thread.shutdown()
                break

            self.completed_segments.append(clip_file_path)

            self.render_thread.shutdown()
        
        self.render_complete = True
        print("tline render done")

    def get_fraction(self):
        return self.render_thread.get_render_fraction()

    def abort(self):
        self.render_thread.shutdown()
        self.aborted = True
        self.thread_running = False

