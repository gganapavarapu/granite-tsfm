import torch
from torch import Tensor
import numpy as np
import pandas as pd

import multiprocessing as mp
from itertools import starmap
from typing import Optional, List, Union, Tuple


class BaseDFDataset(torch.utils.data.Dataset):
    """
    An abtract class representing a :class: `BaseDFDataset`.

    All the datasets that represents data frames should subclass it.
    All subclasses should overwrite :meth: `__get_item__`

    Args:
        data_df (DataFrame, required): input data
        datetime_col (str, optional): datetime column in the data_df. Defaults to None
        x_cols (list, optional): list of columns of X. If x_cols is an empty list, all the columns in the data_df is taken, except the datatime_col. Defaults to an empty list.
        y_cols (list, required): list of columns of y. Defaults to an empty list.
        seq_len (int, required): the sequence length. Defaults to 1
        pred_len (int, required): forecasting horizon. Defaults to 0.
        zero_padding (bool, optional): pad zero if the data_df is shorter than seq_len+pred_len
    """

    def __init__(
        self,
        data_df: pd.DataFrame,
        datetime_col: str = None,
        group_id: Optional[Union[List[int], List[str]]] = None,
        x_cols: list = [],
        y_cols: list = [],
        drop_cols: list = [],
        seq_len: int = 1,
        pred_len: int = 0,
        zero_padding: bool = True,
    ):
        super().__init__()
        if not isinstance(x_cols, list):
            x_cols = [x_cols]
        if not isinstance(y_cols, list):
            y_cols = [y_cols]

        if len(x_cols) > 0:
            assert is_cols_in_df(
                data_df, x_cols
            ), f"one or more {x_cols} is not in the list of data_df columns"

        if len(y_cols) > 0:
            assert is_cols_in_df(
                data_df, y_cols
            ), f"one or more {y_cols} is not in the list of data_df columns"

        if datetime_col:
            assert datetime_col in list(
                data_df.columns
            ), f"{datetime_col} is not in the list of data_df columns"
            assert (
                datetime_col not in x_cols
            ), f"{datetime_col} should not be in the list of x_cols"

        self.data_df = data_df
        self.datetime_col = datetime_col
        self.x_cols = x_cols
        self.y_cols = y_cols
        self.drop_cols = drop_cols
        self.seq_len = seq_len
        self.pred_len = pred_len
        self.zero_padding = zero_padding
        self.timestamps = None
        self.group_id = group_id

        # pad zero to the data_df if the len is shorter than seq_len+pred_len
        # this breaks IDs and timestamps
        if zero_padding:
            data_df = self.pad_zero(data_df)

        # sort the data by datetime
        if datetime_col in list(data_df.columns):
            data_df[datetime_col] = pd.to_datetime(data_df[datetime_col])
            data_df = data_df.sort_values(datetime_col, ignore_index=True)
            self.timestamps = data_df[datetime_col].values

        # get the input data
        if len(x_cols) > 0:
            self.X = data_df[x_cols]
        else:
            drop_cols = self.drop_cols + y_cols
            if datetime_col:
                drop_cols += [datetime_col]
            self.X = data_df.drop(drop_cols, axis=1) if len(drop_cols) > 0 else data_df
            self.x_cols = list(self.X.columns)

        # get target data
        if len(y_cols) > 0:
            self.y = data_df[y_cols]
        else:
            self.y = None

        # get number of X variables
        self.n_vars = self.X.shape[1]
        # get number of target
        self.n_targets = len(y_cols) if len(y_cols) > 0 else 0

    def pad_zero(self, data_df):
        return zero_padding_to_df(data_df, self.seq_len + self.pred_len)

    def __len__(self):
        return len(self.X) - self.seq_len - self.pred_len + 1

    def __getitem__(self, index: int):
        """
        Args:
            index (int): Index
        Returns:
            (Any): Sample and meta data, optionally transformed by the respective transforms.
        """
        raise NotImplementedError


class BaseConcatDFDataset(torch.utils.data.ConcatDataset):
    """
    An abtract class representing a :class: `BaseConcatDFDataset`.

    Args:
        data_df (DataFrame, required): input data
        datetime_col (str, optional): datetime column in the data_df. Defaults to None
        x_cols (list, optional): list of columns of X. If x_cols is an empty list, all the columns in the data_df is taken, except the datatime_col. Defaults to an empty list.
        y_cols (list, required): list of columns of y. Defaults to an empty list.
        group_ids (list, optional): list of group_ids to split the data_df to different groups. If group_ids is defined, it will triggle the groupby method in DataFrame. If empty, entire data frame is treated as one group.
        seq_len (int, required): the sequence length. Defaults to 1
        num_workers (int, optional): the number if workers used for creating a list of dataset from group_ids. Defaults to 1.
        pred_len (int, required): forecasting horizon. Defaults to 0.
        cls (class, required): dataset class
    """

    def __init__(
        self,
        data_df: pd.DataFrame,
        datetime_col: str = None,
        x_cols: list = [],
        y_cols: list = [],
        id_columns: List[str] = [],
        seq_len: int = 1,
        num_workers: int = 1,
        pred_len: int = 0,
        cls=BaseDFDataset,
    ):
        if len(id_columns) > 0:
            assert is_cols_in_df(
                data_df, id_columns
            ), f"{id_columns} is not in the data_df columns"

        self.datetime_col = datetime_col
        self.x_cols = x_cols
        self.y_cols = y_cols
        self.seq_len = seq_len
        self.id_columns = id_columns
        self.num_workers = num_workers
        self.cls = cls
        self.pred_len = pred_len

        # create groupby object
        if len(id_columns) == 1:
            self.group_df = data_df.groupby(by=self.id_columns[0])
        elif len(id_columns) > 1:
            self.group_df = data_df.groupby(by=self.id_columns)
        else:
            data_df["group"] = 0  # create a artificial group
            self.group_df = data_df.groupby(by="group")

        # add group_ids to the drop_cols
        self.drop_cols = id_columns if len(id_columns) > 0 else ["group"]

        self.group_names = list(self.group_df.groups.keys())
        datasets = self.concat_dataset()
        super().__init__(datasets)
        self.n_vars = self.datasets[0].n_vars
        self.n_targets = self.datasets[0].n_targets

    def concat_dataset(self):
        """Create a list of Datasets

        Returns:
            List of datasets
        """
        group_df = self.group_df
        # print(f'group_df: {group_df}')
        # pool = mp.Pool(self.num_workers)
        # pool.starmap(
        list_dset = starmap(
            get_group_data,
            [
                (
                    self.cls,
                    group,
                    group_id,
                    self.datetime_col,
                    self.x_cols,
                    self.y_cols,
                    self.drop_cols,
                    self.seq_len,
                    self.pred_len,
                )
                for group_id, group in group_df
            ],
        )

        # pool.close()
        # del group_df
        return list_dset


def get_group_data(
    cls,
    group,
    group_id,
    datetime_col: str,
    x_cols: list,
    y_cols: list,
    drop_cols: list,
    seq_len: int,
    pred_len: int,
):
    return cls(
        data_df=group,
        group_id=group_id,
        datetime_col=datetime_col,
        x_cols=x_cols,
        y_cols=y_cols,
        drop_cols=drop_cols,
        seq_len=seq_len,
        pred_len=pred_len,
    )


class PretrainDFDataset(BaseConcatDFDataset):
    """
    A :class: `PretrainDFDataset` is used for pretraining.

    To be updated
    Args:
        data_df (DataFrame, required): input data
        datetime_col (str, optional): datetime column in the data_df. Defaults to None
        x_cols (list, optional): list of columns of X. If x_cols is an empty list, all the columns in the data_df is taken, except the datatime_col. Defaults to an empty list.
        group_ids (list, optional): list of group_ids to split the data_df to different groups. If group_ids is defined, it will triggle the groupby method in DataFrame. If empty, entire data frame is treated as one group.
        seq_len (int, required): the sequence length. Defaults to 1
        num_workers (int, optional): the number if workers used for creating a list of dataset from group_ids. Defaults to 1.
    """

    def __init__(
        self,
        data: pd.DataFrame,
        timestamp_column: Optional[str] = None,
        input_columns: List[str] = [],
        id_columns: List[str] = [],
        context_length: int = 1,
        num_workers: int = 1,
    ):
        super().__init__(
            data_df=data,
            datetime_col=timestamp_column,
            x_cols=input_columns,
            id_columns=id_columns,
            seq_len=context_length,
            num_workers=num_workers,
            cls=self.BasePretrainDFDataset,
        )
        self.n_inp = 1

    class BasePretrainDFDataset(BaseDFDataset):
        def __init__(
            self,
            data_df: pd.DataFrame,
            datetime_col: Optional[str] = None,
            group_id: Optional[Union[List[int], List[str]]] = None,
            x_cols: list = [],
            y_cols: list = [],
            drop_cols: list = [],
            seq_len: int = 1,
            pred_len: int = 0,
        ):
            super().__init__(
                data_df=data_df,
                datetime_col=datetime_col,
                group_id=group_id,
                x_cols=x_cols,
                y_cols=y_cols,
                drop_cols=drop_cols,
                seq_len=seq_len,
                pred_len=pred_len,
            )

        def __getitem__(self, time_id):
            seq_x = self.X[time_id : time_id + self.seq_len].values
            ret = {"past_values": np_to_torch(seq_x)}
            if self.datetime_col:
                ret["timestamp"] = self.timestamps[time_id + self.seq_len - 1]
            if self.group_id:
                ret["id"] = self.group_id

            return ret


class ForecastDFDataset(BaseConcatDFDataset):
    """
    A :class: `ForecastDFDataset` used for forecasting.

    Args:
        data_df (DataFrame, required): input data
        datetime_col (str, optional): datetime column in the data_df. Defaults to None
        x_cols (list, optional): list of columns of X. If x_cols is an empty list, all the columns in the data_df is taken, except the datatime_col. Defaults to an empty list.
        group_ids (list, optional): list of group_ids to split the data_df to different groups. If group_ids is defined, it will triggle the groupby method in DataFrame. If empty, entire data frame is treated as one group.
        seq_len (int, required): the sequence length. Defaults to 1
        num_workers (int, optional): the number if workers used for creating a list of dataset from group_ids. Defaults to 1.
        pred_len (int, required): forecasting horizon. Defaults to 0.
    """

    def __init__(
        self,
        data: pd.DataFrame,
        timestamp_column: Optional[str] = None,
        input_columns: List[str] = [],
        output_columns: List[str] = [],
        id_columns: List[str] = [],
        context_length: int = 1,
        prediction_length: int = 1,
        num_workers: int = 1,
    ):
        if output_columns == []:
            output_columns_tmp = input_columns
        else:
            output_columns_tmp = output_columns

        super().__init__(
            data_df=data,
            datetime_col=timestamp_column,
            x_cols=input_columns,
            y_cols=output_columns_tmp,
            id_columns=id_columns,
            seq_len=context_length,
            pred_len=prediction_length,
            num_workers=num_workers,
            cls=self.BaseForecastDFDataset,
        )
        self.n_inp = 2
        # for forecasting, the number of targets is the same as number of X variables
        self.n_targets = self.n_vars

    class BaseForecastDFDataset(BaseDFDataset):
        """
        X_{t+1,..., t+p} = f(X_{:t})
        """

        def __init__(
            self,
            data_df: pd.DataFrame,
            datetime_col: str = None,
            group_id: Optional[Union[List[int], List[str]]] = None,
            x_cols: list = [],
            y_cols: list = [],
            drop_cols: list = [],
            seq_len: int = 1,
            pred_len: int = 1,
        ):
            super().__init__(
                data_df=data_df,
                datetime_col=datetime_col,
                group_id=group_id,
                x_cols=x_cols,
                y_cols=y_cols,
                drop_cols=drop_cols,
                seq_len=seq_len,
                pred_len=pred_len,
            )

        def __getitem__(self, time_id):
            # seq_x: batch_size x seq_len x num_x_cols
            seq_x = self.X[time_id : time_id + self.seq_len].values
            # seq_y: batch_size x pred_len x num_x_cols
            seq_y = self.y[
                time_id + self.seq_len : time_id + self.seq_len + self.pred_len
            ].values

            ret = {
                "past_values": np_to_torch(seq_x),
                "future_values": np_to_torch(seq_y),
            }
            if self.datetime_col:
                ret["timestamp"] = self.timestamps[time_id + self.seq_len - 1]

            if self.group_id:
                ret["id"] = self.group_id

            return ret

        def __len__(self):
            return len(self.X) - self.seq_len - self.pred_len + 1


def np_to_torch(np, float_type=np.float32):
    if np.dtype == "float":
        return torch.from_numpy(np.astype(float_type))
    elif np.dtype == "int":
        return torch.from_numpy(np)


def _torch(*nps):
    return tuple(np_to_torch(x) for x in nps)


def zero_padding_to_df(df: pd.DataFrame, seq_len: int) -> pd.DataFrame:
    """
    check if df has length > seq_len.
    If not, then fill in zero
    Args:
        df (_type_): data frame
        seq_len (int): sequence length
    Returns:
        data frame
    """
    if len(df) >= seq_len:
        return df
    fill_len = seq_len - len(df) + 1
    # add zeros dataframe
    zeros_df = pd.DataFrame(np.zeros([fill_len, df.shape[1]]), columns=df.columns)
    # combine the data
    new_df = pd.concat([zeros_df, df])
    return new_df


def is_cols_in_df(df: pd.DataFrame, cols: List[str]) -> bool:
    """
    Args:
        df:
        cols:

    Returns:
        bool
    """
    for col in cols:
        if col not in list(df.columns):
            return False
    return True


if __name__ == "__main__":
    df = pd.DataFrame(
        {
            "A": [1, 2, 3, 4, 5, 6, 7, 8],
            "B": [4, 5, 6, 7, 8, 9, 10, 11],
            "C": [7, 8, 9, 10, 11, 12, 13, 14],
            "g1": [0, 1, 1, 1, 0, 0, 0, 0],
        }
    )
    print(df)

    d6 = PretrainDFDataset(data_df=df, x_cols=["A", "B"], group_ids=["g1"], seq_len=2)
    print(f"d6: {d6}")

    d7 = ForecastDFDataset(
        data_df=df, x_cols=["A", "B"], group_ids=["g1"], seq_len=2, pred_len=2
    )