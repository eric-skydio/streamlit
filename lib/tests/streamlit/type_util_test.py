# Copyright (c) Streamlit Inc. (2018-2022) Snowflake Inc. (2022)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import unittest
from collections import namedtuple
from datetime import date
from unittest.mock import patch

import numpy as np
import pandas as pd
import plotly.graph_objs as go
import pyarrow as pa
import pytest
from pandas.api.types import infer_dtype
from parameterized import parameterized

from streamlit import type_util
from streamlit.type_util import (
    data_frame_to_bytes,
    fix_arrow_incompatible_column_types,
    is_bytes_like,
    is_snowpark_data_object,
    to_bytes,
)
from tests.streamlit.snowpark_mocks import DataFrame, Row
from tests.testutil import create_snowpark_session

_SHARED_TEST_CASES = [
    # Empty list:
    ([], 0, 0),
    # Empty tuple:
    ((), 0, 0),
    # Empty dict (not a an empty set!)
    ({}, 0, 0),
    # Empty set:
    (set(), 0, 0),
    # 1-dimensional list:
    (["st.text_area", "st.number_input", "st.text_input"], 3, 1),
    # List of integers:
    ([1, 2, 3], 3, 1),
    # List of floats:
    ([1.0, 2.0, 3.0], 3, 1),
    # List of booleans:
    ([True, False, True], 3, 1),
    # List of mixed values:
    ([True, 0, 0.1, "foo"], 4, 1),
    # List of Nones:
    ([None, None, None], 3, 1),
    # List of dates:
    ([date(2020, 1, 1), date(2020, 1, 2), date(2020, 1, 3)], 3, 1),
    # 1-dimensional set:
    ({"st.text_area", "st.number_input", "st.text_input"}, 3, 1),
    # 1-dimensional tuple:
    (("st.text_area", "st.number_input", "st.text_input"), 3, 1),
    # 1-dimensional numpy array:
    (np.array(["st.text_area", "st.number_input", "st.text_input"]), 3, 1),
    (np.array([1, 2, 3]), 3, 1),
    (
        np.array([["st.text_area"], ["st.number_input"], ["st.text_input"]]),
        3,
        1,
    ),
    # Multi-dimensional numpy array:
    (
        np.array(
            [
                ["st.text_area", "widget"],
                ["st.markdown", "element"],
            ]
        ),
        2,
        2,
    ),
    # List[List[Any]]: List of rows
    ([["st.text_area", "widget"], ["st.markdown", "element"]], 2, 2),
    # List[Tuple[Any]]: list of rows
    # TODO(lukasmasuch): Not supported by convert_df_to_reference yet:
    # ([("st.text_area", "widget"), ("st.markdown", "element")], 2, 2),
    # Pandas DataFrame:
    (pd.DataFrame(["st.text_area", "st.markdown"]), 2, 1),
    # Pyarrow Table:
    (pa.Table.from_pandas(pd.DataFrame(["st.text_area", "st.markdown"])), 2, 1),
    # Pandas Series:
    (
        pd.Series(["st.text_area", "st.number_input", "st.text_input"], name="widgets"),
        3,
        1,
    ),
    # [{column -> value}, … , {column -> value}]: List of records:
    (
        [
            {"name": "st.text_area", "type": "widget"},
            {"name": "st.markdown", "type": "element"},
        ],
        2,
        2,
    ),
    # {column -> {index -> value}}: Column-index mapping:
    (
        {
            "type": {"st.text_area": "widget", "st.markdown": "element"},
            "usage": {"st.text_area": 4.92, "st.markdown": 47.22},
        },
        2,
        2,
    ),
    # {column -> [values]}: Column value mapping:
    (
        {
            "name": ["st.text_area", "st.markdown"],
            "type": ["widget", "element"],
        },
        2,
        2,
    ),
    # {column -> Series(values)}: Column series mapping:
    (
        {
            "name": pd.Series(["st.text_area", "st.markdown"], name="name"),
            "type": pd.Series(["widget", "element"], name="type"),
        },
        2,
        2,
    ),
    # {index → value}: Key-value dict
    ({"st.text_area": "widget", "st.markdown": "element"}, 2, 1),
]


class TypeUtilTest(unittest.TestCase):
    def test_list_is_plotly_chart(self):
        trace0 = go.Scatter(x=[1, 2, 3, 4], y=[10, 15, 13, 17])
        trace1 = go.Scatter(x=[1, 2, 3, 4], y=[16, 5, 11, 9])
        data = [trace0, trace1]

        res = type_util.is_plotly_chart(data)
        self.assertTrue(res)

    def test_data_dict_is_plotly_chart(self):
        trace0 = go.Scatter(x=[1, 2, 3, 4], y=[10, 15, 13, 17])
        trace1 = go.Scatter(x=[1, 2, 3, 4], y=[16, 5, 11, 9])
        d = {"data": [trace0, trace1]}

        res = type_util.is_plotly_chart(d)
        self.assertTrue(res)

    def test_dirty_data_dict_is_not_plotly_chart(self):
        trace0 = go.Scatter(x=[1, 2, 3, 4], y=[10, 15, 13, 17])
        trace1 = go.Scatter(x=[1, 2, 3, 4], y=[16, 5, 11, 9])
        d = {"data": [trace0, trace1], "foo": "bar"}  # Illegal property!

        res = type_util.is_plotly_chart(d)
        self.assertFalse(res)

    def test_layout_dict_is_not_plotly_chart(self):
        d = {
            # Missing a component with a graph object!
            "layout": {"width": 1000}
        }

        res = type_util.is_plotly_chart(d)
        self.assertFalse(res)

    def test_fig_is_plotly_chart(self):
        trace1 = go.Scatter(x=[1, 2, 3, 4], y=[16, 5, 11, 9])

        # Plotly 3.7 needs to read the config file at /home/.plotly when
        # creating an image. So let's mock that part of the Figure creation:
        with patch("plotly.offline.offline._get_jconfig") as mock:
            mock.return_value = {}
            fig = go.Figure(data=[trace1])

        res = type_util.is_plotly_chart(fig)
        self.assertTrue(res)

    def test_is_namedtuple(self):
        Boy = namedtuple("Boy", ("name", "age"))
        John = Boy("John", "29")

        res = type_util.is_namedtuple(John)
        self.assertTrue(res)

    def test_to_bytes(self):
        bytes_obj = b"some bytes"
        self.assertTrue(is_bytes_like(bytes_obj))
        self.assertIsInstance(to_bytes(bytes_obj), bytes)

        bytearray_obj = bytearray("a bytearray string", "utf-8")
        self.assertTrue(is_bytes_like(bytearray_obj))
        self.assertIsInstance(to_bytes(bytearray_obj), bytes)

        string_obj = "a normal string"
        self.assertFalse(is_bytes_like(string_obj))
        with self.assertRaises(RuntimeError):
            to_bytes(string_obj)

    def test_data_frame_with_dtype_values_to_bytes(self):
        df1 = pd.DataFrame(["foo", "bar"])
        df2 = pd.DataFrame(df1.dtypes)

        try:
            data_frame_to_bytes(df2)
        except Exception as ex:
            self.fail(f"Converting dtype dataframes to Arrow should not fail: {ex}")

    @parameterized.expand(
        [(None, 0, 0)] + _SHARED_TEST_CASES,
    )
    def test_convert_anything_to_df(
        self,
        input_data: type_util.DataFrameCompatible,
        expected_rows: int,
        expected_cols: int,
    ):
        """Test that `convert_anything_to_df` correctly converts
        a variety of types to a DataFrame.
        """
        converted_df = type_util.convert_anything_to_df(input_data)
        self.assertEqual(converted_df.shape[0], expected_rows)
        self.assertEqual(converted_df.shape[1], expected_cols)

    @parameterized.expand(_SHARED_TEST_CASES)
    def test_convert_df_to_reference(
        self,
        input_data: type_util.DataFrameCompatible,
        expected_rows: int,
        expected_cols: int,
    ):
        """Test that `convert_df_to_reference` correctly converts a DataFrame
        to the same type and structure of the reference.
        """

        converted_df = type_util.convert_anything_to_df(input_data)
        self.assertEqual(converted_df.shape[0], expected_rows)
        self.assertEqual(converted_df.shape[1], expected_cols)

        converted_data = type_util.convert_df_to_reference(converted_df, input_data)
        self.assertEqual(type(input_data), type(converted_data))
        self.assertEqual(str(input_data), str(converted_data))
        self.assertTrue(
            converted_df.equals(type_util.convert_anything_to_df(converted_data))
        )

    def test_fix_complex_column_type(self):
        """Test that `fix_unsupported_column_types` correctly fixes
        columns containing complex types by converting them to string.
        """
        df = pd.DataFrame(
            {
                "complex": [1 + 2j, 3 + 4j, 5 + 6 * 1j],
                "integer": [1, 2, 3],
                "string": ["foo", "bar", None],
            }
        )

        self.assertEqual(infer_dtype(df["complex"]), "complex")

        fixed_df = fix_arrow_incompatible_column_types(df)
        self.assertEqual(infer_dtype(fixed_df["complex"]), "string")

    def test_fix_mixed_column_types(self):
        """Test that `fix_arrow_incompatible_column_types` correctly fixes
        columns containing mixed types by converting them to string.
        """
        df = pd.DataFrame(
            {
                "mixed-integer": [1, "foo", 3],
                "mixed": [1.0, "foo", 3],
                "integer": [1, 2, 3],
                "float": [1.0, 2.1, 3.2],
                "string": ["foo", "bar", None],
            },
            index=[1.0, "foo", 3],
        )

        fixed_df = fix_arrow_incompatible_column_types(df)

        self.assertEqual(infer_dtype(fixed_df["mixed-integer"]), "string")
        self.assertEqual(infer_dtype(fixed_df["mixed"]), "string")
        self.assertEqual(infer_dtype(fixed_df["integer"]), "integer")
        self.assertEqual(infer_dtype(fixed_df["float"]), "floating")
        self.assertEqual(infer_dtype(fixed_df["string"]), "string")
        self.assertEqual(infer_dtype(fixed_df.index), "string")

        self.assertEqual(
            str(fixed_df.dtypes),
            """mixed-integer     object
mixed             object
integer            int64
float            float64
string            object
dtype: object""",
        )

    def test_data_frame_with_unsupported_column_types(self):
        """Test that `data_frame_to_bytes` correctly handles dataframes
        with unsupported column types by converting those types to string.
        """
        df = pd.DataFrame(
            {
                "mixed-integer": [1, "foo", 3],
                "mixed": [1.0, "foo", 3],
                "complex": [1 + 2j, 3 + 4j, 5 + 6 * 1j],
                "integer": [1, 2, 3],
                "float": [1.0, 2.1, 3.2],
                "string": ["foo", "bar", None],
            },
            index=[1.0, "foo", 3],
        )

        try:
            data_frame_to_bytes(df)
        except Exception as ex:
            self.fail(
                "No exception should have been thrown here. "
                f"Unsupported types of this dataframe should have been automatically fixed: {ex}"
            )

    def test_is_snowpark_dataframe(self):
        df = pd.DataFrame(
            {
                "mixed-integer": [1, "foo", 3],
                "mixed": [1.0, "foo", 3],
                "complex": [1 + 2j, 3 + 4j, 5 + 6 * 1j],
                "integer": [1, 2, 3],
                "float": [1.0, 2.1, 3.2],
                "string": ["foo", "bar", None],
            },
            index=[1.0, "foo", 3],
        )

        # pandas dataframe should not be SnowparkDataFrame
        self.assertFalse(is_snowpark_data_object(df))

        # if snowflake.snowpark.dataframe.DataFrame def is_snowpark_data_object should return true
        self.assertTrue(is_snowpark_data_object(DataFrame()))

        # any object should not be snowpark dataframe
        self.assertFalse(is_snowpark_data_object("any text"))
        self.assertFalse(is_snowpark_data_object(123))

        class DummyClass:
            """DummyClass for testing purposes"""

        self.assertFalse(is_snowpark_data_object(DummyClass()))

        # empty list should not be snowpark dataframe
        self.assertFalse(is_snowpark_data_object(list()))

        # list with items should not be snowpark dataframe
        self.assertFalse(
            is_snowpark_data_object(
                [
                    "any text",
                ]
            )
        )
        self.assertFalse(
            is_snowpark_data_object(
                [
                    123,
                ]
            )
        )
        self.assertFalse(
            is_snowpark_data_object(
                [
                    DummyClass(),
                ]
            )
        )
        self.assertFalse(
            is_snowpark_data_object(
                [
                    df,
                ]
            )
        )

        # list with SnowparkRow should be SnowparkDataframe
        self.assertTrue(
            is_snowpark_data_object(
                [
                    Row(),
                ]
            )
        )

    @pytest.mark.require_snowflake
    def test_is_snowpark_dataframe_integration(self):
        with create_snowpark_session() as snowpark_session:
            self.assertTrue(
                is_snowpark_data_object(snowpark_session.sql("SELECT 40+2 as COL1"))
            )
            self.assertTrue(
                is_snowpark_data_object(
                    snowpark_session.sql("SELECT 40+2 as COL1").collect()
                )
            )
            self.assertTrue(
                is_snowpark_data_object(
                    snowpark_session.sql("SELECT 40+2 as COL1").cache_result()
                )
            )
