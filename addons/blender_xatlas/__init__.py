# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import importlib
import os
import platform
import string
import struct
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from io import StringIO
from queue import Empty, Queue
from threading import Thread
from typing import List

import bmesh
import bpy

bl_info = {
    "name": "Blender Xatlas",
    "description": "Unwrap Objects with Xatlas, 'A cleaned up version of thekla_atlas'",
    "author": "mattedickson",
    "wiki_url": "https://github.com/mattedicksoncom/blender-xatlas/",
    "tracker_url": "https://github.com/mattedicksoncom/blender-xatlas/issues",
    "version": (0, 0, 14),
    "blender": (5, 1, 0),
    "location": "3D View > Toolbox",
    "category": "Object",
}

# make sure __path__ is not a list here, to prevent crash when using blender_vscode
__safe_path__ = __path__
if type(__safe_path__) == list:
    __safe_path__ = __safe_path__[0]

sys.path.append(__safe_path__)

from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import (
    AddonPreferences,
    Operator,
    Panel,
    PropertyGroup,
)
from bpy.utils import register_class, unregister_class

addon_name = __name__


# Python 3.13 (Blender 5.1) uses lazy annotations, so __annotations__ on an
# instance no longer works - use the class instead, with bl_rna as fallback.
def _prop_keys(pg_instance):
    cls = type(pg_instance)
    ann = getattr(cls, "__annotations__", None)
    if ann:
        return list(ann.keys())
    return [
        p.identifier
        for p in pg_instance.bl_rna.properties
        if p.identifier != "rna_type"
    ]


# begin PropertyGroups---------------------------
class PG_PackProperties(PropertyGroup):
    bruteForce: BoolProperty(
        name="Brute Force",
        description="Slower, but gives the best result. If false, use random chart placement.",
        default=False,
    )

    resolution: IntProperty(
        name="Texture Resolution (px)",
        description="Resolution of goal texture",
        default=256,
        min=0,
        max=4096,
    )

    padding: IntProperty(
        name="Padding Amount (px)",
        description="Pixels to pad each uv island",
        default=2,
        min=0,
        max=64,
    )

    bilinear: BoolProperty(
        name="Bilinear",
        description="Leave space around pack for bilinear filtering",
        default=True,
    )

    blockAlign: BoolProperty(
        name="blockAlign",
        description="Align charts to 4x4 blocks. Also improves packing speed, since there are fewer possible chart locations to consider.",
        default=False,
    )

    maxChartSize: IntProperty(
        name="maxChartSize",
        description="Charts larger than this will be scaled down. 0 means no limit.",
        default=0,
        min=0,
        max=10000,
    )

    texelsPerUnit: FloatProperty(
        name="texelsPerUnit",
        description="Unit to texel scale. e.g. a 1x1 quad with texelsPerUnit of 32 will take up approximately 32x32 texels in the atlas.\nIf resolution is also 0, the estimated value will approximately match a 1024x1024 atlas.",
        default=0.0,
        min=0.0,
        max=10000.0,
    )


class PG_ChartProperties(PropertyGroup):
    maxChartArea: FloatProperty(
        name="maxChartArea",
        description="Don't grow charts to be larger than this. 0 means no limit.",
        default=0.0,
        min=0.0,
        max=10000.0,
    )
    maxBoundaryLength: FloatProperty(
        name="maxBoundaryLength",
        description="Don't grow charts to have a longer boundary than this. 0 means no limit.",
        default=0.0,
        min=0.0,
        max=10000.0,
    )

    normalDeviationWeight: FloatProperty(
        name="normalDeviationWeight",
        description="Angle between face and average chart normal.",
        default=2.0,
        min=0.0,
        max=10000.0,
    )
    roundnessWeight: FloatProperty(
        name="roundnessWeight", description="TODO", default=0.01, min=0.0, max=10000.0
    )
    straightnessWeight: FloatProperty(
        name="straightnessWeight", description="TODO", default=6.0, min=0.0, max=10000.0
    )
    normalSeamWeight: FloatProperty(
        name="normalSeamWeight",
        description="If > 1000, normal seams are fully respected.",
        default=4.0,
        min=0.0,
        max=10000.0,
    )
    textureSeamWeight: FloatProperty(
        name="textureSeamWeight",
        description="If > 1000, normal seams are fully respected.",
        default=0.5,
        min=0.0,
        max=10000.0,
    )

    maxCost: FloatProperty(
        name="maxCost",
        description="If total of all metrics * weights > maxCost, don't grow chart. Lower values result in more charts.",
        default=2.0,
        min=0.0,
        max=10000.0,
    )

    maxIterations: IntProperty(
        name="maxIterations",
        description="Number of iterations of the chart growing and seeding phases. Higher values result in better charts.",
        default=1,
        min=0,
        max=1000,
    )


def get_collectionNames(self, context):
    colllectionNames = []
    for collection in bpy.data.collections:
        colllectionNames.append((collection.name, collection.name, ""))
    return colllectionNames


def gen_safe_name():
    genId = uuid.uuid4().hex
    return "u_" + genId


class PG_SharedProperties(PropertyGroup):
    unwrapSelection: EnumProperty(
        name="",
        description="Which Objects to unwrap",
        items=[
            ("SELECTED", "Selection", ""),
            ("ALL", "All", ""),
            ("COLLECTION", "Collection", ""),
        ],
    )

    atlasLayout: EnumProperty(
        name="",
        description="How to Layout the atlases",
        items=[
            ("OVERLAP", "Overlap", "Overlap all the atlases"),
            ("SPREADX", "Spread X", "Seperate each atlas along the x-axis"),
            ("UDIM", "UDIM", "Lay the atlases out for UDIM"),
        ],
    )

    selectedCollection: EnumProperty(name="", items=get_collectionNames)

    mainUVIndex: IntProperty(
        name="",
        description="The index of the primary none lightmap uv",
        default=0,
        min=0,
        max=1000,
    )

    lightmapUVIndex: IntProperty(
        name="", description="The index of the lightmap uv", default=0, min=0, max=1000
    )

    mainUVChoiceType: EnumProperty(
        name="",
        description="The method to obtain the main UV",
        items=[
            ("NAME", "By Name", ""),
            ("INDEX", "By Index", ""),
        ],
    )

    mainUVName: StringProperty(
        name="",
        description="The name of the main (non-lightmap) UV",
        default="UVMap",
    )

    lightmapUVChoiceType: EnumProperty(
        name="",
        description="The method to obtain the lightmap UV",
        items=[
            ("NAME", "By Name", ""),
            ("INDEX", "By Index", ""),
        ],
    )

    lightmapUVName: StringProperty(
        name="",
        description="The name of the lightmap UV (If it doesn't exist it will be created)",
        default="UVMap_Lightmap",
    )

    packOnly: BoolProperty(
        name="Pack Only",
        description="Don't unwrap the meshes, only, pack them",
        default=False,
    )

    makeSingleUserCopy: BoolProperty(
        name="Make Single User Copy",
        description="Make shared mesh data single-user before unwrapping. If off, shared meshes are only unwrapped once.",
        default=True,
    )

    individualAtlasPerObject: BoolProperty(
        name="Individual Atlas Per Object",
        description="Each object will be unwrapped separately using the full atlas space. Useful for exporting to game engines.",
        default=False,
    )


# end PropertyGroups---------------------------


# begin operators------------------------------
class Setup_Unwrap(bpy.types.Operator):
    bl_idname = "object.setup_unwrap"
    bl_label = "Select the objects to be unwrapped"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        sharedProperties = bpy.context.scene.shared_properties

        # save whatever mode the user was in
        startingMode = bpy.context.object.mode
        startingSelection = bpy.context.selected_objects
        startingActiveObject = context.view_layer.objects.active
        bpy.ops.object.mode_set(mode="OBJECT")

        # get all the currently selected objects
        selected_objects = None
        if sharedProperties.unwrapSelection == "SELECTED":
            selected_objects = bpy.context.selected_objects
        elif sharedProperties.unwrapSelection == "ALL":
            bpy.ops.object.select_all(action="DESELECT")
            for object in bpy.context.scene.objects:
                current_object = object
                if current_object.type == "MESH":
                    current_object.select_set(True)
            selected_objects = bpy.context.selected_objects
        elif sharedProperties.unwrapSelection == "COLLECTION":
            bpy.ops.object.select_all(action="DESELECT")
            for collection in bpy.data.collections:
                if collection.name == sharedProperties.selectedCollection:
                    for current_object in collection.all_objects:
                        if current_object.type == "MESH":
                            current_object.select_set(True)
            selected_objects = bpy.context.selected_objects

        if sharedProperties.individualAtlasPerObject:
            # dedup shared mesh data across the batch
            seen_mesh_data = set()
            for obj in selected_objects:
                if obj.type == "MESH" and not sharedProperties.makeSingleUserCopy:
                    if obj.data.users > 1 and obj.data.name in seen_mesh_data:
                        print(
                            f"Skipping '{obj.name}', shares mesh data with an already-unwrapped object"
                        )
                        continue
                    seen_mesh_data.add(obj.data.name)

                bpy.ops.object.select_all(action="DESELECT")
                obj.select_set(True)
                context.view_layer.objects.active = obj
                Unwrap_Lightmap_Group_Xatlas_2.execute(self, context)
        else:
            Unwrap_Lightmap_Group_Xatlas_2.execute(self, context)

        # reset everything
        bpy.ops.object.select_all(action="DESELECT")
        for objects in startingSelection:
            objects.select_set(True)
        context.view_layer.objects.active = startingActiveObject
        bpy.ops.object.mode_set(mode=startingMode)

        return {"FINISHED"}


# Unwrap Lightmap Group Xatlas
class Unwrap_Lightmap_Group_Xatlas_2(bpy.types.Operator):
    bl_idname = "object.unwrap_lightmap_group_xatlas_2"
    bl_label = "Unwrap Lightmap Group Xatlas"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        packOptions = bpy.context.scene.pack_tool
        chartOptions = bpy.context.scene.chart_tool
        sharedProperties = bpy.context.scene.shared_properties

        startingMode = bpy.context.object.mode
        selected_objects = bpy.context.selected_objects

        if len(selected_objects) == 0:
            print("Nothing Selected")
            self.report({"WARNING"}, "Nothing Selected, please select Something")
            return {"FINISHED"}

        rename_dict = dict()
        safe_dict = dict()

        # remember original active UV index to restore later
        original_active_uv = dict()

        # dedup shared mesh data when makeSingleUserCopy is off
        seen_mesh_data = set()
        skipped_objects = []

        # make sure all the objects have lightmap uvs
        for obj in selected_objects:
            if obj.type == "MESH":
                if not sharedProperties.makeSingleUserCopy:
                    if obj.data.users > 1:
                        if obj.data.name in seen_mesh_data:
                            # already unwrapped this mesh data
                            skipped_objects.append(obj)
                            obj.select_set(False)
                            continue
                        seen_mesh_data.add(obj.data.name)

                safe_name = gen_safe_name()
                rename_dict[obj.name] = (obj.name, safe_name)
                safe_dict[safe_name] = obj.name
                context.view_layer.objects.active = obj
                if sharedProperties.makeSingleUserCopy and obj.data.users > 1:
                    obj.data = obj.data.copy()
                uv_layers = obj.data.uv_layers

                # keyed by mesh data name so shared data records once
                if obj.data.name not in original_active_uv:
                    original_active_uv[obj.data.name] = uv_layers.active_index

                uvName = "UVMap_Lightmap"
                if sharedProperties.lightmapUVChoiceType == "NAME":
                    uvName = sharedProperties.lightmapUVName
                elif sharedProperties.lightmapUVChoiceType == "INDEX":
                    if sharedProperties.lightmapUVIndex < len(uv_layers):
                        uvName = uv_layers[sharedProperties.lightmapUVIndex].name

                if uvName not in uv_layers:
                    uvmap = uv_layers.new(name=uvName)
                    uv_layers.active_index = len(uv_layers) - 1
                else:
                    for i in range(0, len(uv_layers)):
                        if uv_layers[i].name == uvName:
                            uv_layers.active_index = i
                obj.select_set(True)
            else:
                # deselect non-mesh objects, can't triangulate/export them
                obj.select_set(False)

        # re-point active object if it got deselected above
        remaining = [o for o in bpy.context.selected_objects if o.type == "MESH"]
        if remaining and context.view_layer.objects.active not in remaining:
            context.view_layer.objects.active = remaining[0]

        if skipped_objects:
            skipped_names = ", ".join(o.name for o in skipped_objects)
            print(
                f"Skipped {len(skipped_objects)} object(s) sharing already-unwrapped mesh data: {skipped_names}"
            )

        selected_objects = bpy.context.selected_objects

        if len(selected_objects) == 0:
            print("Nothing left to unwrap")
            return {"FINISHED"}

        # save all the current edges (pack-only mode)
        if sharedProperties.packOnly:
            edgeDict = dict()
            for obj in selected_objects:
                if obj.type == "MESH":
                    tempEdgeDict = dict()
                    tempEdgeDict["object"] = obj.name
                    tempEdgeDict["edges"] = []
                    print(len(obj.data.edges))
                    for i in range(0, len(obj.data.edges)):
                        tempEdgeDict["edges"].append(i)
                    edgeDict[obj.name] = tempEdgeDict

        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.mesh.quads_convert_to_tris(quad_method="FIXED", ngon_method="BEAUTY")
        bpy.ops.object.mode_set(mode="OBJECT")

        # export to a real temp file, Blender 5.x needs a real filepath
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".obj")
        os.close(tmp_fd)

        bpy.ops.wm.obj_export(
            filepath=tmp_path,
            export_animation=False,
            apply_modifiers=False,  # must match obj.data topology, UVs are written back onto it
            export_eval_mode="DAG_EVAL_VIEWPORT",
            export_selected_objects=True,
            export_uv=True,
            export_normals=True,
            export_materials=False,
            export_triangulated_mesh=False,
            export_curves_as_nurbs=False,
            export_object_groups=False,  # keep "o" names as bare object names
            export_material_groups=False,
            export_vertex_groups=False,
            export_smooth_groups=False,
            smooth_group_bitflags=False,
        )

        with open(tmp_path, "r") as f:
            fakeFile_value = f.read()

        try:
            os.remove(tmp_path)
        except OSError:
            pass

        # get the path to xatlas
        file_path = os.path.dirname(os.path.abspath(__file__))
        if platform.system() == "Windows":
            xatlas_path = os.path.join(file_path, "xatlas", "xatlas-blender.exe")
        elif platform.system() == "Linux":
            xatlas_path = os.path.join(file_path, "xatlas", "xatlas-blender")
            subprocess.Popen('chmod u+x "' + xatlas_path + '"', shell=True)
        elif platform.system() == "Darwin":
            xatlas_path = os.path.join(file_path, "xatlas", "xatlas-blender")
            subprocess.Popen('chmod u+x "' + xatlas_path + '"', shell=True)

        # build xatlas argument string using _prop_keys() for Py 3.13 compat
        arguments_string = ""
        for argumentKey in _prop_keys(packOptions):
            attrib = getattr(packOptions, argumentKey)
            if type(attrib) == bool:
                if attrib:
                    arguments_string += " -" + argumentKey
            else:
                arguments_string += " -" + argumentKey + " " + str(attrib)

        for argumentKey in _prop_keys(chartOptions):
            attrib = getattr(chartOptions, argumentKey)
            if type(attrib) == bool:
                if attrib:
                    arguments_string += " -" + argumentKey
            else:
                arguments_string += " -" + argumentKey + " " + str(attrib)

        if sharedProperties.packOnly:
            arguments_string += " -packOnly"

        arguments_string += " -atlasLayout " + sharedProperties.atlasLayout

        print(arguments_string)

        # RUN xatlas process
        xatlas_process = subprocess.Popen(
            r'"{}"'.format(xatlas_path) + " " + arguments_string,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            shell=True,
        )

        stdin = xatlas_process.stdin
        value = bytes(fakeFile_value + "\n", "UTF-8")
        stdin.write(value)
        stdin.flush()

        outObj = ""
        while True:
            output = xatlas_process.stdout.readline()
            if not output:
                break
            outObj += output.decode().strip() + "\n"

        # parse xatlas output
        @dataclass
        class uvObject:
            obName: string = ""
            uvArray: List[float] = field(default_factory=list)
            faceArray: List[int] = field(default_factory=list)

        convertedObjects = []
        uvArrayComplete = []

        obTest = None
        startRead = False
        for line in outObj.splitlines():
            line_split = line.split()
            if not line_split:
                continue
            line_start = line_split[0]

            if line_start == "STARTOBJ":
                print(
                    "Start reading the objects----------------------------------------"
                )
                startRead = True

            if startRead:
                if line_start == "o":
                    if obTest is not None:
                        convertedObjects.append(obTest)
                    obTest = uvObject()
                    obTest.obName = line_split[1]

                if obTest is not None:
                    if line_start == "vt":
                        newUv = [float(line_split[1]), float(line_split[2])]
                        obTest.uvArray.append(newUv)
                        uvArrayComplete.append(newUv)

                    if line_start == "f":
                        newFace = [
                            int(line_split[1].split("/")[1]),
                            int(line_split[2].split("/")[1]),
                            int(line_split[3].split("/")[1]),
                        ]
                        obTest.faceArray.append(newFace)

        if obTest is not None:
            convertedObjects.append(obTest)

        # apply the UVs back to the scene objects
        print("Applying the UVs----------------------------------------")
        for importObject in convertedObjects:
            bpy.ops.object.select_all(action="DESELECT")
            obTest = importObject

            # resolve xatlas's "o" name back to the scene object
            raw_name = obTest.obName
            resolved = safe_dict.get(raw_name, raw_name)

            if resolved not in bpy.context.scene.objects:
                # fallback: try stripping prefix/suffix decorations
                candidate = None
                for sep in ("_", "/", "."):
                    if sep in raw_name:
                        parts = raw_name.split(sep)
                        suffix = parts[-1]
                        prefix = parts[0]
                        if suffix in bpy.context.scene.objects:
                            candidate = suffix
                            break
                        if prefix in bpy.context.scene.objects:
                            candidate = prefix
                            break
                if candidate is None:
                    for obj_name in safe_dict.values():
                        if raw_name.startswith(obj_name) or raw_name.endswith(obj_name):
                            candidate = obj_name
                            break
                resolved = candidate if candidate else resolved

            obTest.obName = resolved

            if obTest.obName not in bpy.context.scene.objects:
                print(
                    f"Warning: object '{raw_name}' (resolved: '{obTest.obName}') not found in scene, skipping."
                )
                continue

            bpy.context.scene.objects[obTest.obName].select_set(True)
            context.view_layer.objects.active = bpy.context.scene.objects[obTest.obName]
            bpy.ops.object.mode_set(mode="OBJECT")

            obj = bpy.context.active_object
            me = obj.data
            bm = bmesh.new()
            bm.from_mesh(me)
            uv_layer = bm.loops.layers.uv.verify()
            nFaces = len(bm.faces)
            if hasattr(bm.faces, "ensure_lookup_table"):
                bm.faces.ensure_lookup_table()

            for faceIndex in range(nFaces):
                faceGroup = obTest.faceArray[faceIndex]
                bm.faces[faceIndex].loops[0][uv_layer].uv = (
                    uvArrayComplete[faceGroup[0] - 1][0],
                    uvArrayComplete[faceGroup[0] - 1][1],
                )
                bm.faces[faceIndex].loops[1][uv_layer].uv = (
                    uvArrayComplete[faceGroup[1] - 1][0],
                    uvArrayComplete[faceGroup[1] - 1][1],
                )
                bm.faces[faceIndex].loops[2][uv_layer].uv = (
                    uvArrayComplete[faceGroup[2] - 1][0],
                    uvArrayComplete[faceGroup[2] - 1][1],
                )

            bm.to_mesh(me)
            bm.free()

            # restore original active UV map
            original_index = original_active_uv.get(me.name)
            if original_index is not None and 0 <= original_index < len(me.uv_layers):
                me.uv_layers.active_index = original_index

        # restore quads in pack-only mode
        if sharedProperties.packOnly:
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.select_all(action="DESELECT")
            bpy.ops.object.mode_set(mode="OBJECT")

            for edges in edgeDict:
                edgeList = edgeDict[edges]
                currentObject = bpy.context.scene.objects[edgeList["object"]]
                bm = bmesh.new()
                bm.from_mesh(currentObject.data)
                if hasattr(bm.edges, "ensure_lookup_table"):
                    bm.edges.ensure_lookup_table()

                newEdges = []
                for edge in range(len(edgeList["edges"]), len(bm.edges)):
                    newEdge = bm.edges[edge]
                    newEdge.select = True
                    newEdges.append(newEdge)

                bmesh.ops.dissolve_edges(
                    bm, edges=newEdges, use_verts=False, use_face_split=False
                )
                bpy.ops.object.mode_set(mode="OBJECT")
                bm.to_mesh(currentObject.data)
                bm.free()
                bpy.ops.object.mode_set(mode="EDIT")

        # re-select the originally selected objects
        for objectName in rename_dict:
            if objectName in bpy.context.scene.objects:
                current_object = bpy.context.scene.objects[objectName]
                current_object.select_set(True)
                context.view_layer.objects.active = current_object

        bpy.ops.object.mode_set(mode=startingMode)
        print("Finished Xatlas----------------------------------------")
        return {"FINISHED"}


# end operators------------------------------


# begin panels------------------------------
class OBJECT_PT_xatlas_panel(Panel):
    bl_idname = "OBJECT_PT_xatlas_panel"
    bl_label = "Xatlas Tools"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Xatlas"
    bl_context = ""

    @classmethod
    def poll(self, context):
        return context.object is not None

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        packtool = scene.pack_tool
        mytool = scene.chart_tool


class OBJECT_PT_pack_panel(Panel):
    bl_idname = "OBJECT_PT_pack_panel"
    bl_label = "Pack Options"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Xatlas"
    bl_parent_id = "OBJECT_PT_xatlas_panel"
    bl_context = ""

    @classmethod
    def poll(self, context):
        return context.object is not None

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        packtool = scene.pack_tool

        box = layout.box()

        for tool in _prop_keys(packtool):
            box.prop(packtool, tool)


class OBJECT_PT_chart_panel(Panel):
    bl_idname = "OBJECT_PT_chart_panel"
    bl_label = "Chart Options"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Xatlas"
    bl_parent_id = "OBJECT_PT_xatlas_panel"
    bl_context = ""

    @classmethod
    def poll(self, context):
        return context.object is not None

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        mytool = scene.chart_tool

        box = layout.box()

        for tool in _prop_keys(mytool):
            box.prop(mytool, tool)


class OBJECT_PT_run_panel(Panel):
    bl_idname = "OBJECT_PT_run_panel"
    bl_label = "Run Xatlas"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Xatlas"
    bl_parent_id = "OBJECT_PT_xatlas_panel"
    bl_context = ""

    @classmethod
    def poll(self, context):
        return context.object is not None

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        box = layout.box()
        row = box.row()
        row.label(text="Unwrap")
        row.prop(scene.shared_properties, "unwrapSelection")
        if scene.shared_properties.unwrapSelection == "COLLECTION":
            box.prop(scene.shared_properties, "selectedCollection")

        box = layout.box()
        row = box.row()
        row.label(text="Lightmap UV")
        row.prop(scene.shared_properties, "lightmapUVChoiceType")
        if scene.shared_properties.lightmapUVChoiceType == "NAME":
            box.prop(scene.shared_properties, "lightmapUVName")
        elif scene.shared_properties.lightmapUVChoiceType == "INDEX":
            box.prop(scene.shared_properties, "lightmapUVIndex")

        box = layout.box()
        row = box.row()
        row.label(text="Main UV")
        row.prop(scene.shared_properties, "mainUVChoiceType")
        if scene.shared_properties.mainUVChoiceType == "NAME":
            box.prop(scene.shared_properties, "mainUVName")
        elif scene.shared_properties.mainUVChoiceType == "INDEX":
            box.prop(scene.shared_properties, "mainUVIndex")

        box = layout.box()
        row = box.row()
        row.label(text="Atlas Layout")
        row.prop(scene.shared_properties, "atlasLayout")

        box.operator("object.setup_unwrap", text="Run Xatlas")

        row = box.row()
        row.prop(scene.shared_properties, "packOnly")
        row = box.row()
        row.prop(scene.shared_properties, "individualAtlasPerObject")
        row = box.row()
        row.prop(scene.shared_properties, "makeSingleUserCopy")


# end panels------------------------------

# begin setup------------------------------

classes = (
    PG_SharedProperties,
    PG_PackProperties,
    PG_ChartProperties,
    Setup_Unwrap,
    Unwrap_Lightmap_Group_Xatlas_2,
    OBJECT_PT_xatlas_panel,
    OBJECT_PT_pack_panel,
    OBJECT_PT_chart_panel,
    OBJECT_PT_run_panel,
)


def register():
    for cls in classes:
        register_class(cls)

    bpy.types.Scene.pack_tool = PointerProperty(type=PG_PackProperties)
    bpy.types.Scene.chart_tool = PointerProperty(type=PG_ChartProperties)
    bpy.types.Scene.shared_properties = PointerProperty(type=PG_SharedProperties)


def unregister():
    for cls in reversed(classes):
        unregister_class(cls)

    del bpy.types.Scene.shared_properties
    del bpy.types.Scene.chart_tool
    del bpy.types.Scene.pack_tool


if __name__ == "__main__":
    pass
    # register()
# end setup------------------------------
